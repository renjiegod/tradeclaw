export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        "shell-bg": "#f4efe6",
        "shell-ink": "#1d1a16",
        "card-bg": "#fffdf9",
        "shell-line": "#e8ddd0",
        "shell-muted": "#746b61",
        "shell-accent": "#c98536",
        "soft-tag-border": "#e6c8a0",
        "soft-tag-text": "#8d5f2a",
        "soft-tag-bg": "#f8eddc",
      },
      fontFamily: {
        sans: ["IBM Plex Sans", "sans-serif"],
        display: ["Fraunces", "serif"],
      },
      boxShadow: {
        "shell-card": "0 12px 34px rgba(68, 46, 20, 0.06)",
      },
    },
  },
};
