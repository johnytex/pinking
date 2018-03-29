#! /bin/bash

# This script launches two ipfs daemons with separate repositories and
# runs through ipfs client commands either directly to one of the daemons,
# or through local_proxy -> server_proxy, django -> ipfs daemon 2, and then
# compares the output for different tests

# Kill all ipfs processes and local/server proxy
ps -A | grep ipfs | awk '{print $1}' | xargs -I {} kill -9 {} 2> /dev/null
# Kill proxies
ps -A | grep local_proxy | awk '{print $1}' | xargs -I {} kill -9 {} 2> /dev/null
ps -A | grep server_proxy | awk '{print $1}' | xargs -I {} kill -9 {} 2> /dev/null
# Kill django
ps -A | grep runserver | awk '{print $1}' | xargs -I {} kill -9 {} 2> /dev/null

# Launch two ipfs daemons with different 
IPFS1=/tmp/.pinking_ipfs1
IPFS2=/tmp/.pinking_ipfs2
IPFS1_PORT=5001
IPFS2_PORT=5002
PRE=/ip4/127.0.0.1/tcp/
IPFS1_API=$PRE$IPFS1_PORT 
IPFS2_API=$PRE$IPFS2_PORT 
PROXY_PORT=5003
PROXY_API=$PRE$PROXY_PORT 
SERVER_PROXY_PORT=5004
DJANGO_PORT=8000
rm -rf $IPFS1 2> /dev/null
rm -rf $IPFS2 2> /dev/null
IPFS_PATH=$IPFS1 ipfs --api $IPFS1_API daemon --init > /dev/null 2>&1 &
IPFS_PATH=$IPFS2 ipfs --api $IPFS2_API init > /dev/null 2>&1
# Change the port of gateways so they don't collide
sed -i -e 's/8080/8081/g' $IPFS2/config
IPFS_PATH=$IPFS2 ipfs --api $IPFS2_API daemon > /dev/null 2>&1 &
sleep 10

# Flush the django database
python manage.py flush --noinput
# Create a super user account
echo "from django.contrib.auth.models import User; \
  User.objects.create_superuser('test', 'test@example.com', 'test')"\
  | python manage.py shell
python manage.py runserver > /dev/null 2>&1 &

python proxies/local_proxy.py --listen_port $PROXY_PORT \
                              --target_url http://127.0.0.1:$SERVER_PROXY_PORT \
                              -u test \
                              -p test > /dev/null 2>&1 &
python proxies/server_proxy.py --listen_port $SERVER_PROXY_PORT \
                               --ipfs_port $IPFS2_PORT \
                               --django_port $DJANGO_PORT > /dev/null 2>&1 &
sleep 3

function kill_all {
  sleep 1
  kill %4
  kill %5
  kill %1
  kill %2
  kill %3
}

function test {
  # Run command against both apis and compare
  echo "Running 'ipfs $1'"
  OUT1=$(ipfs --api $IPFS1_API $1 2> errfile1)
  ERR1=$(< errfile1)
  OUT2=$(ipfs --api $PROXY_API $1 2> errfile2)
  ERR2=$(< errfile2)
  if [ "$OUT1" != "$OUT2" ]; then
    echo "STDOUT: ipfs $1 differed between node and proxy"
    echo "IPFS: $OUT1 != PROXY: $OUT2"
    kill_all
    exit 1
  fi
  if [ "$ERR1" != "$ERR2" ]; then
    echo "STDERR: ipfs $1 differed between node and proxy"
    echo "IPFS: $ERR1 != PROXY: $ERR2"
    kill_all
    exit 1
  fi
  TEST_STDOUT=$OUT1
  TEST_STDERR=$ERR1
  echo "Result: $OUT1 $ERR1"
}

# First remove pins that automatically come with the repos
ipfs --api $IPFS1_API pin ls | grep recursive | cut -d " " -f 1 | xargs -I {} ipfs --api $IPFS1_API pin rm {}
ipfs --api $IPFS2_API pin ls | grep recursive | cut -d " " -f 1 | xargs -I {} ipfs --api $IPFS2_API pin rm {}

test "pin ls"

#Initialize a repository
echo "hello world" > testfile
test "add testfile"
HASH=$(echo $TEST_STDOUT | cut -d " " -f 2)

test "pin ls" # testfile should be pinned recursively

test "pin rm $HASH"
test "pin ls"

test "pin add $HASH"
test "pin ls"

test "pin add --recursive=false $HASH" # should fail
test "pin rm $HASH" # delete the recursive one
test "pin add --recursive=false $HASH" # should work now

echo "hello world2" > testfile2
test "add testfile2"
HASH2=$(echo $TEST_STDOUT | cut -d " " -f 2)

test "pin ls"
test "pin rm $HASH $HASH2"
test "pin ls"

echo "All tests passed"

# Kill all processes we started in the beginning
kill_all