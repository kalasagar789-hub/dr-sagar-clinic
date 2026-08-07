import type { Config } from "tailwindcss";
export default { content:["./index.html","./src/**/*.{ts,tsx}"], theme:{extend:{colors:{medical:{blue:"#1565C0",teal:"#26A69A",canvas:"#F8FAFC"}},boxShadow:{card:"0 4px 20px rgba(15, 23, 42, .06)"}}}, plugins:[] } satisfies Config;
