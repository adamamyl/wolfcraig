remote_smtp_dkim:
  driver = smtp
  hosts_try_fastopen = *

  hosts_require_tls = *
  tls_verify_hosts = *
  tls_verify_certificates = /etc/ssl/certs/ca-certificates.crt

  dkim_domain      = $${lookup{$$sender_address_domain}lsearch{${dkim_base}/keymap}}
  dkim_selector    = ${dkim_selector}
  dkim_private_key = $${lookup{$$sender_address_domain}lsearch{${dkim_base}/keymap}{${dkim_base}/$$value/private.key}{}}
  dkim_canon       = relaxed
  dkim_sign_headers = Date:From:To:Subject:Message-ID:Content-Type
