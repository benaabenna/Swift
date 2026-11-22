module.exports = {
    content: { relative: true, files: ['./templates/**/*.html'] },
    theme: {
        extend: {
            fontFamily: { display: ['system-ui', 'sans-serif'] },
            colors: {
                navy: { 950: '#061021', 900: '#0A192F', 800: '#112240', 700: '#1D3557' }
            }
        }
    }
};
