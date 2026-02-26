primary_hostname = ${primary_hostname}

tls_advertise_hosts = *
tls_certificate = /etc/exim4/certs/${primary_domain}/cert.pem
tls_privatekey  = /etc/exim4/certs/${primary_domain}/key.pem
