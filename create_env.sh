ENV=".env"
if ! [ -f $ENV ]; then
  NON_INTERACTIVE_FLAG=$1
  
  # create env file and write Airflow UID ang GID to it
  echo -e "AIRFLOW_UID=$(id -u)\nAIRFLOW_GID=0" > $ENV

  # generate or read API key
  PSEUDORANDOM_API_KEY=$( dd bs=512 if=/dev/urandom count=1 2>/dev/null | tr -dc '[:alpha:]' | fold -w20 | head -n 1 )
  if ! [ $NON_INTERACTIVE_FLAG == "--non-interactive" ]; then
    echo "Choose API key generation method."
    select yn in "User-provided" "Pseudorandom"; do
        case $yn in
            User-provided ) read -p "Enter API KEY: " API_KEY; break;;
            Pseudorandom ) API_KEY=$PSEUDORANDOM_API_KEY; echo "API key: ${API_KEY}"; break;;
        esac
    done
  else
    API_KEY=$PSEUDORANDOM_API_KEY
  fi
  # append API key to env file
  echo -e "TIGHTLOCK_API_KEY=$API_KEY" >> $ENV
fi