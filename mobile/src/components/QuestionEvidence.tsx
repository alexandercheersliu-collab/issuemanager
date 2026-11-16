import { useState } from 'react'
import { Download, FileText } from 'lucide-react'
import { toast } from 'sonner'
import AuthImage from '@/components/AuthImage'
import { Button } from '@/components/ui/button'
import { api } from '@/lib/api'
import type { Question } from '@/types'

export default function QuestionEvidence({
  question,
  imageAlt = '错题原图',
  showDocument = true,
}: {
  question: Question
  imageAlt?: string
  showDocument?: boolean
}) {
  const [downloading, setDownloading] = useState(false)

  if (!question.image_path) return null
  if (question.evidence_type === 'document' && !showDocument) return null
  if (question.evidence_type !== 'document') {
    return (
      <AuthImage
        src={`/questions/${question.id}/image`}
        alt={imageAlt}
        fallback={<p className="text-xs text-amber-700">原始证据暂不可用</p>}
      />
    )
  }

  async function download() {
    setDownloading(true)
    try {
      const url = await api.assetUrl(`/questions/${question.id}/image`)
      if (!url) throw new Error('原始文档暂不可用')
      const link = document.createElement('a')
      link.href = url
      link.download = question.evidence_filename || 'original-evidence.docx'
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '下载失败，请重试')
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
      <div className="flex min-w-0 items-center gap-2 text-sm text-slate-700">
        <FileText className="h-5 w-5 shrink-0 text-blue-600" />
        <span>原始文档</span>
        <span className="rounded bg-white px-1.5 py-0.5 text-xs font-medium text-slate-500">
          DOCX
        </span>
      </div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={downloading}
        onClick={download}
      >
        <Download className="mr-1.5 h-4 w-4" />
        {downloading ? '下载中…' : '下载原始文档'}
      </Button>
    </div>
  )
}
