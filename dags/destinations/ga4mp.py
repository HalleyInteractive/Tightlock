"""
Copyright 2023 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

     https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License."""

"""GA4 MP destination implementation."""

# pylint: disable=raise-missing-from

import datetime
import enum
import json
import logging
from typing import (
    Annotated,
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import errors
import immutabledict
import requests
from pydantic import BaseModel, Field
from utils import RunResult, ValidationResult


class GA4Base(BaseModel):
  type: Literal["GA4MP"] = "GA4MP"
  api_secret: str
  non_personalized_ads: Optional[bool] = False
  debug: Optional[bool] = False
  user_properties: Optional[Dict[str, Dict[str, str]]]


class GA4Web(GA4Base):
  event_type: Literal["gtag"]
  measurement_id: str


class GA4App(GA4Base):
  event_type: Literal["firebase"]
  firebase_app_id: str


_GA_EVENT_POST_URL = "https://www.google-analytics.com/mp/collect"
_GA_EVENT_VALIDATION_URL = "https://www.google-analytics.com/debug/mp/collect"

_FIREBASE_ID_COLUMN = "app_instance_id"
_GTAG_ID_COLUMN = "client_id"

_ERROR_TYPES = immutabledict.immutabledict({
    "client_id": errors.ErrorNameIDMap.GA4_HOOK_ERROR_VALUE_REQUIRED_CLIENT_ID,
    "user_id": errors.ErrorNameIDMap.GA4_HOOK_ERROR_VALUE_INVALID_USER_ID,
    "timestamp_micros": errors.ErrorNameIDMap.GA4_HOOK_ERROR_VALUE_INVALID_TIMESTAMP_MICROS,
    "user_properties": errors.ErrorNameIDMap.GA4_HOOK_ERROR_VALUE_INVALID_USER_PROPERTIES,
    "non_personalized_ads": errors.ErrorNameIDMap.GA4_HOOK_ERROR_VALUE_INVALID_NON_PERSONALIZED_ADS,
    "events": errors.ErrorNameIDMap.GA4_HOOK_ERROR_VALUE_INVALID_EVENTS,
    "events.params": errors.ErrorNameIDMap.GA4_HOOK_ERROR_VALUE_INVALID_EVENTS_PARAMS,
    "events.params.items": errors.ErrorNameIDMap.GA4_HOOK_ERROR_VALUE_INVALID_EVENTS_PARAMS_ITEMS,
})


class PayloadTypes(enum.Enum):
  """GA4 Measurememt Protocol supported payload types."""

  FIREBASE = "firebase"
  GTAG = "gtag"


class Destination:
  """Implements DestinationProto protocol for GA4 Measurement Protocol."""

  def __init__(self, config: Dict[str, Any]):
    self.config = config  # Keeping a reference for convenience.
    self.payload_type = config.get("payload_type")
    self.api_secret = config.get("api_secret")
    if self.payload_type == PayloadTypes.FIREBASE.value:
      self.firebase_app_id = config.get("firebase_app_id")
    elif self.payload_type == PayloadTypes.GTAG.value:
      self.measurement_id = config.get("measurement_id")
    self.non_personalized_ads = config.get("non_personalized_ads") or False
    self.user_properties = config.get("user_properties") or None
    self.debug = config.get("debug") or False

    self._validate_credentials()
    self.post_url = self._build_api_url(True)
    self.validate_url = self._build_api_url(False)

  def _get_valid_and_invalid_events(
      self, events: List[Dict[str, Any]]
  ) -> Tuple[List[Tuple[int, Dict[str, Any]]], List[Tuple[int, errors.ErrorNameIDMap]]]:
    """Prepares index-event tuples to keep order while sending.

       Validation of the payload is done by posting it to the validation API
       provided by the product of Google Analytics 4.

       A valid payload format in event should comply with rules described below.
       https://developers.google.com/analytics/devguides/collection/protocol/ga4/reference/events

       For the limitations of the payload, please refer to:
       https://developers.google.com/analytics/devguides/collection/protocol/ga4/sending-events?client_type=gtag#limitations

    Args:
      events: Events to prepare for sending.

    Returns:
      A list of index-event tuples for the valid events, and a list of
      index-error for the invalid events.
    """

    valid_events = []
    invalid_indices_and_errors = []
    for i, event in enumerate(events):
      payload = {}
      if self.payload_type == PayloadTypes.FIREBASE.value:
        payload["app_instance_id"] = event.get("app_instance_id", "")
      elif self.payload_type == PayloadTypes.GTAG.value:
        payload["client_id"] = str(event.get("client_id", ""))
      payload["user_id"] = str(event.get("user_id", ""))
      payload["non_personalized_ads"] = self.non_personalized_ads
      if self.user_properties:
        payload["user_properties"] = self.user_properties
      payload["events"] = [{
          "name": event.get("event_name", ""),
          "params": {
              "engagement_time_msec": event.get("engagement_time_msec", ""),
              "session_id": event.get("session_id", ""),
          },
      }]

      try:
        response = self._send_validate_request(payload)
        self._parse_validate_result(event, response)
        valid_events.append((i, payload))
      except errors.DataOutConnectorValueError as error:
        invalid_indices_and_errors.append((i, error.error_num))

    return valid_events, invalid_indices_and_errors

  def _parse_validate_result(
      self,
      event: Dict[str, Any],  # pylint: disable=unused-argument
      response: requests.Response,
  ):
    """Parses the response returned from the debug API.

       The response body contains a JSON to indicate the validated result.
       For example:
       {
         "validationMessages": [
          {
            "fieldPath": "timestamp_micros"
            "description": "Measurement timestamp_micros has timestamp....",
            "validationCode": "VALUE_INVALID"
          }]
       }

       The fieldPath indicates which part of your payload JSON contains invalid
       value, when fieldPath doesn't exist, the fieldPath can be found in
       description as well.

    Args:
      event: The event that contains the index and the payload.
      response: The HTTP response from the debug API.
    """
    if response.status_code >= 500:
      raise errors.DataOutConnectorValueError(
          error_num=errors.ErrorNameIDMap.RETRIABLE_GA4_HOOK_ERROR_HTTP_ERROR
      )
    elif response.status_code != 200:
      raise errors.DataOutConnectorValueError(
          error_num=errors.ErrorNameIDMap.NON_RETRIABLE_ERROR_EVENT_NOT_SENT
      )

    try:
      validation_result = response.json()
    except json.JSONDecodeError as err:
      raise errors.DataOutConnectorValueError(
          error_num=errors.ErrorNameIDMap.RETRIABLE_GA4_HOOK_ERROR_HTTP_ERROR.value
      ) from err

    # Payload is valid: validation messages are only returned if there is a
    # problem with the payload.
    if not validation_result["validationMessages"]:
      return

    # The validation API only ever returns one message.
    message = validation_result["validationMessages"][0]
    field_path = message.get("fieldPath", "")
    description = message.get("description", "")

    for property_name in _ERROR_TYPES:
      if field_path == property_name or property_name in description:
        raise errors.DataOutConnectorValueError(error_num=_ERROR_TYPES[property_name])

    # Prevent from losing error message if it is undefined due to API change.
    # Note: TCRM has an "id" field which we don't have:
    # go/tcrm-install#prepare-data-to-send-to-google-analytics-4
    logging.error("fieldPath: %s, description: %s", field_path, description)
    raise errors.DataOutConnectorValueError(
        error_num=errors.ErrorNameIDMap.GA4_HOOK_ERROR_INVALID_VALUES
    )

  def _send_validate_request(self, payload: Dict[str, Any]) -> requests.Response:
    """Sends the GA4 payload to the debug API for data validating.

       By adding the key-value pair
       (validationBehavior: ENFORCE_RECOMMENDATIONS), the API will check the
       payload thoroughly, this is recommended because the Measurement Protocol
       API won't check the data and it fails silently, you might not know what
       happened to your data.

    Args:
       payload: the JSON payload of the GA4 event.

    Returns:
       The response from the debug API.
    """
    validating_payload = dict(payload)
    validating_payload["validationBehavior"] = "ENFORCE_RECOMMENDATIONS"
    try:
      response = requests.post(self.validate_url, json=validating_payload)
    except requests.ConnectionError as err:
      raise errors.DataOutConnectorValueError(
          error_num=errors.ErrorNameIDMap.RETRIABLE_GA4_HOOK_ERROR_HTTP_ERROR
      ) from err
    return response

  def _send_payload(self, payload: Dict[str, Any]) -> None:
    """Sends payload to GA via Measurement Protocol REST API.

    Args:
      payload: Parameters containing required data for app conversion tracking.

    Returns:
      results: Includes request body, status_code, error_msg, response body and
      debugg flag.
      The response refers to the definition of conversion tracking response in
      https://developers.google.com/analytics/devguides/collection/protocol/ga4/reference?client_type=firebase
    """

    if self.debug:
      self.log.info(
          """Debug mode: Simulating sending event to GA4 (data will not
          actually be sent). URL:{}. payload data:{}.""".format(
              self.post_url, payload
          )
      )
      return

    try:
      response = requests.post(self.post_url, json=payload)
      # Success is to be considered between 200 and 299:
      # https://developer.mozilla.org/en-US/docs/Web/HTTP/Status
      if response.status_code < 200 or response.status_code >= 300:
        raise errors.DataOutConnectorSendUnsuccessfulError(
            msg="Sending payload to GA did not complete successfully.",
            error_num=errors.ErrorNameIDMap.RETRIABLE_GA_HOOK_ERROR_HTTP_ERROR,
        )
    except requests.ConnectionError:
      raise errors.DataOutConnectorSendUnsuccessfulError(
          msg="Sending payload to GA did not complete successfully.",
          error_num=errors.ErrorNameIDMap.RETRIABLE_GA_HOOK_ERROR_HTTP_ERROR,
      )

  def send_data(
      self, input_data: List[Mapping[str, Any]], dry_run: bool
  ) -> Optional[RunResult]:
    """Builds payload ans sends data to GA4MP API."""
    valid_events, invalid_indices_and_errors = self._get_valid_and_invalid_events(
        input_data
    )

    if not dry_run:
      for valid_event in valid_events:
        try:
          event = valid_event[1]
          self._send_payload(event)
        except (
            errors.DataOutConnectorSendUnsuccessfulError,
            errors.DataOutConnectorValueError,
        ) as error:
          index = valid_event[0]
          invalid_indices_and_errors.append((index, error.error_num))
    else:
      print(
          "Dry-Run: Events will be validated agains the debug endpoint and will not be actually sent."
      )

    print(f"Valid events: {valid_events}")
    print(f"Invalid events: {invalid_indices_and_errors}")

    for invalid_event in invalid_indices_and_errors:
      event_index = invalid_event[0]
      error_num = invalid_event[1]
      # TODO(b/272258038): TBD What to do with invalid events data.
      print(f"event_index: {event_index}; error_num: {error_num}")

    run_result = RunResult(
        successful_hits=len(valid_events),
        failed_hits=len(invalid_indices_and_errors),
        error_messages=[str(error[1]) for error in invalid_indices_and_errors],
        dry_run=dry_run,
    )

    return run_result

  def schema(self) -> Dict[str, Any]:
    GA4MP = Annotated[Union[GA4Web, GA4App], Field(discriminator="event_type")]

    return GA4MP.schema_json()

  def fields(self) -> Sequence[str]:
    if self.payload_type == PayloadTypes.FIREBASE.value:
      id_column_name = _FIREBASE_ID_COLUMN
    else:
      id_column_name = _GTAG_ID_COLUMN
    return [id_column_name] + [
        "user_id",
        "event_name",
        "engagement_time_msec",
        "session_id",
    ]

  def batch_size(self) -> int:
    return 10000

  def validate(self) -> ValidationResult:
    timestamp_micros = int(datetime.datetime.now().timestamp() * 1e6)
    payload = {
        "timestamp_micros": timestamp_micros,
        "non_personalized_ads": False,
        "events": [],
        "validationBehavior": "ENFORCE_RECOMMENDATIONS",
    }
    if self.payload_type == PayloadTypes.GTAG.value:
      payload["client_id"] = "validation_client_id"
    else:
      payload[
          "app_instance_id"
      ] = "cccccccccccccccccccccccccccccccc"  # 32 digit app_instance_id
    validation_response = self._send_validate_request(payload).json()
    validation_messages = validation_response["validationMessages"]
    if len(validation_messages) < 1:
      return ValidationResult(True, [])
    else:
      return ValidationResult(False, validation_messages)

  def _validate_credentials(self) -> None:
    """Validate credentials.

    Raises:
      Exception: If credential combination does not meet criteria.
    """
    if not self.api_secret:
      raise errors.DataOutConnectorValueError(
          f"Missing API secret in config: {self.config}"
      )

    valid_payload_types = (PayloadTypes.FIREBASE.value, PayloadTypes.GTAG.value)
    if self.payload_type not in valid_payload_types:
      raise errors.DataOutConnectorValueError(
          f"Unsupport payload_type: {self.payload_type}. Supported "
          "payload_type is gtag or firebase."
      )

    if self.payload_type == PayloadTypes.FIREBASE.value and not self.firebase_app_id:
      raise errors.DataOutConnectorValueError(
          "Wrong payload_type or missing firebase_app_id. Please make sure "
          "firebase_app_id is set when payload_type is firebase."
      )

    if self.payload_type == PayloadTypes.GTAG.value and not self.measurement_id:
      raise errors.DataOutConnectorValueError(
          "Wrong payload_type or missing measurement_id. Please make sure "
          "measurement_id is set when payload_type is gtag."
      )

  def _build_api_url(self, is_post: bool) -> str:
    """Builds the url for sending the payload.

    Args:
      is_post: true for building post url, false for building validation url.
    Returns:
      url: Full url that can be used for sending the payload
    """
    if self.payload_type == PayloadTypes.GTAG.value:
      query_url = "api_secret={}&measurement_id={}".format(
          self.api_secret, self.measurement_id
      )
    else:
      query_url = "api_secret={}&firebase_app_id={}".format(
          self.api_secret, self.firebase_app_id
      )
    if is_post:
      built_url = f"{_GA_EVENT_POST_URL}?{query_url}"
    else:
      built_url = f"{_GA_EVENT_VALIDATION_URL}?{query_url}"
    return built_url
