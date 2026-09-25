// Named rather than exported anonymously so a stack trace from the PostCSS
// pipeline points at something.
const config = { plugins: { tailwindcss: {}, autoprefixer: {} } };
export default config;
