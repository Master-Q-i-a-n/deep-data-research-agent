#!/bin/sh
set -eu

secret_path=/run/secrets/redis_password
acl_path=/run/redis/users.acl

if [ ! -s "$secret_path" ]; then
  echo "Redis password secret is missing or empty" >&2
  exit 1
fi

password="$(tr -d '\r\n' < "$secret_path")"
if [ "${#password}" -lt 32 ]; then
  echo "Redis password must contain at least 32 characters" >&2
  exit 1
fi

umask 077
chown root:redis /run/redis
chmod 0750 /run/redis
{
  echo "user default off"
  printf '%s\n' "user ddra on >${password} ~ddra:* &* +ping +time +eval +evalsha +script|load +script|exists +zadd +zrem +zremrangebyscore +zcard +zrange +zscore +pexpire +pttl +set +get +getdel +del +unlink +hget +hset +hmget"
} > "$acl_path"
chown redis:redis "$acl_path"

exec /usr/bin/setpriv --reuid redis --regid redis --clear-groups \
  redis-server /usr/local/etc/redis/redis.conf
