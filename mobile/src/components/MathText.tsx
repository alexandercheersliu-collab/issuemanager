/** Markdown + LaTeX 公式渲染（懒加载）。
 *
 * react-markdown + KaTeX 体积约 300KB，拆成独立 chunk 按需加载；
 * chunk 加载期间先用纯文本兜底（内容可读，公式显示源码）。
 *
 * 归一化：AI 输出的公式定界符不统一，\( \) \[ \] 一律转成 $...$ / $$...$$。
 */
import { lazy, Suspense } from 'react'

const Inner = lazy(() => import('./MathTextInner'))

export function normalizeMathDelimiters(text: string): string {
  return text
    .replace(/\\\[([\s\S]*?)\\\]/g, (_, m) => `$$${m}$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_, m) => `$${m}$`)
}

export default function MathText({
  text,
  className,
}: {
  text: string
  className?: string
}) {
  const normalized = normalizeMathDelimiters(text)
  return (
    <Suspense
      fallback={
        <p className={`whitespace-pre-wrap ${className ?? ''}`}>{normalized}</p>
      }
    >
      <Inner text={normalized} className={className} />
    </Suspense>
  )
}
