#!/bin/bash

PSQL="psql --username=freecodecamp --dbname=number_guess -t --no-align -c"

echo "Enter your username:"
read USERNAME

USER_QUERY=$($PSQL "SELECT user_id, games_played, best_game FROM users WHERE username='$USERNAME'")