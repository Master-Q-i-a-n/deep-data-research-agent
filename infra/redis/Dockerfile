FROM redis:7.4-alpine@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf

COPY redis.conf /usr/local/etc/redis/redis.conf
COPY entrypoint.sh /usr/local/bin/ddra-redis-entrypoint.sh

RUN chmod 0555 /usr/local/bin/ddra-redis-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/ddra-redis-entrypoint.sh"]
