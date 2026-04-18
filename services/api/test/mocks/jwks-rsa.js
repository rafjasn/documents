'use strict';

class JwksClient {
    constructor() {}
    getSigningKey(_kid, cb) {
        cb(null, { getPublicKey: () => '' });
    }
}

module.exports = {
    JwksClient,
    passportJwtSecret: () => () => {}
};
