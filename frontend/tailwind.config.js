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
        // Chat surface — a cool, calm blue sub-theme kept separate from the
        // warm shell palette so the conversation reads like a distinct surface
        // without leaking hardcoded hex into className strings.
        chat: {
          bubble: "#eaf4ff",
          ink: "#07122e",
          muted: "#7f8493",
          line: "#e7e9ef",
          accent: "#246bfe",
          hover: "#f4f6fb",
          surface: "#f8fafc",
        },
      },
      borderRadius: {
        // Converge the ad-hoc rounded-[20px] / rounded-[24px] / rounded-[28px]
        // sprinkled across cards / modals / bubbles into a small named scale.
        card: "16px",
        modal: "20px",
        bubble: "20px",
        chat: "28px",
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
  plugins: [require("@tailwindcss/typography")],
};
