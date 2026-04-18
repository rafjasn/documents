module.exports = function (options) {
    const existingExternals = options.externals;

    function swaggerUiDistExternal({ request }, callback) {
        if (request === 'swagger-ui-dist' || request.startsWith('swagger-ui-dist/')) {
            return callback(null, 'commonjs ' + request);
        }

        callback();
    }

    let externals;

    if (!existingExternals) {
        externals = swaggerUiDistExternal;
    } else if (Array.isArray(existingExternals)) {
        externals = [...existingExternals, swaggerUiDistExternal];
    } else {
        externals = [existingExternals, swaggerUiDistExternal];
    }

    return { ...options, externals };
};
