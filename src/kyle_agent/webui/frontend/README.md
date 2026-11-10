# kyleAgent Web Console 前端(React + Vite + Tailwind + shadcn/ui)

Web Console 的前端源码。**构建产物 `../dist/` 已提交入库**——运行
`kyle-webui` 与三平台 CI 只需 Python,不需要 Node;只有改前端源码时才需要
Node ≥ 20 与本目录的 npm 依赖。

## 技术栈

- Vite + React + TypeScript(严格模式,`npm run build` 先 `tsc -b` 类型检查)
- Tailwind CSS v4(`@tailwindcss/vite` 插件,主题变量在 `src/index.css`)
- shadcn/ui(radix 栈,按需组件在 `src/components/ui/`)+ lucide-react 图标
- zustand(连接/会话/OnCall 状态)、react-markdown + remark-gfm + remark-breaks
- 零运行时外部网络依赖:系统字体栈(不引 CDN 字体),图标本地打包

## 命令

```bash
npm install        # 首次安装(依赖锁定在 package-lock.json)
npm run dev        # 开发模式:http://127.0.0.1:5173
npm run build      # 类型检查 + 构建到 ../dist(vite.build.outDir)
npm run preview    # 本地预览构建产物
```

## 开发模式代理

`vite.config.ts` 把 `/ws`(WebSocket,ws:true)与 `/api` 代理到
`http://127.0.0.1:8080`——即 `kyle-webui` 的默认端口。开发时照常起后端:

```bash
uv run kyle-core                 # 终端 1:daemon(7437)
uv run --extra webui kyle-webui  # 终端 2:桥接器(8080)
cd src/kyle_agent/webui/frontend && npm run dev   # 终端 3:前端(5173)
# 浏览器打开 http://127.0.0.1:5173(WS 经 vite 代理到桥接器)
```

## 构建产物入库纪律

- `vite build` 输出到 `../dist/`(`emptyOutDir`),产物必须随前端源码变更
  一并提交——`tests/unit/test_webui_dist.py` 会校验 `dist/index.html` 与其
  引用的 `/assets/*` 真实存在,漏 build/漏提交时该测试最先红。
- `../bridge.py` 只 serve `dist/`(import 期 mount `dist/assets`,目录缺失
  会直接炸,同样有测试守护)。
- 本目录不进 wheel(pyproject `[tool.hatch.build.targets.wheel] exclude`)。

## 主题

`index.html` 内联脚本在 CSS 生效前设置主题(防闪屏):localStorage key
`kyle.theme`(与旧版无构建前端兼容,用户偏好不丢),首访跟随
`prefers-color-scheme`。shadcn 变量体系:`.dark` 类切换,同时写
`data-theme` 保持旧语义;语义状态色(ok/warn/err/info)双主题各自派生。

## 目录速览

```
src/
├── App.tsx                  # 骨架:顶栏/侧栏/会话流/输入区 + 审批键盘
├── index.css                # v3「control-room」token(色板/字体/动效/md-body)
├── lib/                     # theme、format(previewOf/heroGreet 等纯函数)
├── store/                   # app(连接/主题)、session(事件状态机)、
│                            # oncall(轮询/演练/报告)、composer、toast
├── ws/                      # client(JSON-RPC over WS,1.6s 重连)、session 引导
└── components/
    ├── topbar/              # 品牌+环境徽章+主题+水位+cache+连接指示
    ├── sidebar/             # 新会话/演练/告警/健康/报告/会话操作/脚注
    ├── transcript/          # 会话流:Markdown/工具卡/审批卡/子代理/Hero
    ├── composer/            # 自动增高输入区(Enter 发送/Shift+Enter 换行)
    ├── oncall/              # 演练菜单/告警队列/服务健康/报告列表/浮层
    ├── common/              # toast 宿主
    └── ui/                  # shadcn 生成组件
```
