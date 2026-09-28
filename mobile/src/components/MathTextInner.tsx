/** MathText 的实际渲染层（独立 chunk：react-markdown + KaTeX + 样式）。 */
import ReactMarkdown from 'react-markdown'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'

export default function MathTextInner({
  text,
  className,
}: {
  text: string
  className?: string
}) {
  return (
    <div className={`math-text ${className ?? ''}`}>
      <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
