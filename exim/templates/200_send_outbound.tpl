send_outbound:
  driver = dnslookup
  domains = ! +local_domains
  transport = remote_smtp_dkim
  ignore_target_hosts = 0.0.0.0 : 127.0.0.0/8
  no_more
