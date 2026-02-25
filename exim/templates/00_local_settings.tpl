primary_hostname = ${primary_hostname}

local_interfaces = 127.0.0.1 : ::1 : ${relay_subnet}

tls_advertise_hosts = *
tls_certificate = /etc/exim4/certs/${primary_domain}/cert.pem
tls_privatekey  = /etc/exim4/certs/${primary_domain}/key.pem

log_selector = +smtp_connection +tls_peerdn +tls_sni +sender_on_delivery

qualify_domain = ${primary_domain}
