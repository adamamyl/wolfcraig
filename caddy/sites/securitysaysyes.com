wolfmail.securitysaysyes.com {
    respond "." 200
}

mta-sts.securitysaysyes.com {
    handle /.well-known/mta-sts.txt {
        respond `version: STSv1
mode: enforce
mx: wolfmail.securitysaysyes.com.
max_age: 86400` 200
    }
    respond 404
}
