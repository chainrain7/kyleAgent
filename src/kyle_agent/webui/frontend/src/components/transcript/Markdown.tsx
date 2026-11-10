/* Markdown 渲染:react-markdown + remark-gfm(标题/列表/围栏/链接/表格)
 * + remark-breaks(单换行 → <br>,对齐旧版逐行渲染,报告的 结论/处置/结果
 *   各占一行)。样式走 .md-body(对齐旧 assistant-text)。 */
import ReactMarkdown from "react-markdown"
import remarkBreaks from "remark-breaks"
import remarkGfm from "remark-gfm"

// 对话与报告共用的 markdown 容器
export function Markdown({ children }: { children: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{children}</ReactMarkdown>
    </div>
  )
}
