remote_smtp_dkim:
  driver = smtp
  hosts_try_fastopen = *

  hosts_require_tls = *
  tls_verify_hosts = *
  tls_verify_certificates = /etc/ssl/certs/ca-certificates.crt

  tls_certificate = /etc/exim4/certs/$${sender_address_domain}/cert.pem
  tls_privatekey  = /etc/exim4/certs/$${sender_address_domain}/key.pem

  dkim_domain      = $${sender_address_domain}
  dkim_selector    = mail
  dkim_private_key = ${dkim_base}/$${sender_address_domain}/private.key
  dkim_canon       = relaxed
  dkim_sign_headers = Date:From:To:Subject:Message-ID:Content-Type
