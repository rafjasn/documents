module.exports = (options) => ({
  ...options,
  externals: [
    ...(Array.isArray(options.externals) ? options.externals : []),
    { sharp: 'commonjs sharp' },
    { 'pdf-parse': 'commonjs pdf-parse' },
  ],
});
