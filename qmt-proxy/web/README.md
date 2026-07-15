# qmt-proxy Web UI

基于 React、TypeScript、Vite 和 Ant Design 的前端工作台。

## Scripts

```bash
npm install
npm run dev
npm run build
npm run preview
npm run test
```

## Development

- 开发服务器默认把 `/api` 和 `/ws` 代理到 `http://127.0.0.1:8000`
- 生产构建默认输出到 `web/dist`
- FastAPI 会在主服务的 `/ui` 路径托管构建产物
