import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {  
    host: true  
},
  base: process.env.VITE_BASE_PATH || "/neuropdfv2",
  optimizeDeps: {
    exclude: ["lucide-react"],
  },
});
