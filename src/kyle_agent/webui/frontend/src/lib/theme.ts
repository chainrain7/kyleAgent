/* 主题工具:与 index.html 内联预设脚本同语义,读写 localStorage key「kyle.theme」
 * (保持用户已有偏好数据),首访跟随 prefers-color-scheme。 */
export type Theme = "dark" | "light"

// 读取初始主题:localStorage 优先,否则跟随系统(暗色为默认)
export function initialTheme(): Theme {
  try {
    const t = localStorage.getItem("kyle.theme")
    if (t === "dark" || t === "light") return t
  } catch {
    /* 隐私模式等场景静默 */
  }
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"
}

// 应用主题:类名驱动 shadcn 变量,data-theme 保持旧版语义,并持久化
export function applyTheme(theme: Theme): void {
  const root = document.documentElement
  root.classList.toggle("dark", theme === "dark")
  root.dataset.theme = theme
  try {
    localStorage.setItem("kyle.theme", theme)
  } catch {
    /* 静默 */
  }
}

// 切换主题并加 180ms 颜色过渡类(结束后移除,避免影响动画性能)
export function toggleTheme(): Theme {
  const cur: Theme = document.documentElement.classList.contains("dark") ? "dark" : "light"
  const next: Theme = cur === "dark" ? "light" : "dark"
  const root = document.documentElement
  root.classList.add("theme-switching")
  applyTheme(next)
  window.setTimeout(() => root.classList.remove("theme-switching"), 200)
  return next
}
