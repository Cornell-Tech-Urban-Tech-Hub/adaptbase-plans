import { defineConfig } from 'astro/config';

// https://astro.build/config
export default defineConfig({
  site: 'https://plans.adaptbase.us',
  output: 'static',
  build: {
    inlineStylesheets: 'auto'
  },
  vite: {
    css: {
      devSourcemap: true
    }
  }
});
