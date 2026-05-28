# Pin Playwright base image to the same version as the npm package so the
# bundled browser binaries match and no extra download happens at runtime.
FROM mcr.microsoft.com/playwright:v1.49.0-jammy

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund \
 && npm cache clean --force

COPY src ./src

USER pwuser
CMD ["node", "src/index.js"]
