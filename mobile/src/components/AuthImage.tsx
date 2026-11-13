import { useEffect, useState, type ReactNode } from 'react'
import { api } from '@/lib/api'

export default function AuthImage({
  src,
  alt,
  fallback = null,
}: {
  src: string
  alt: string
  fallback?: ReactNode
}) {
  const [result, setResult] = useState<{ src: string; url: string | null } | null>(null)

  useEffect(() => {
    let active = true
    let objectUrl: string | null = null

    void api.assetUrl(src).then((next) => {
      if (!active) {
        if (next) URL.revokeObjectURL(next)
        return
      }
      objectUrl = next
      setResult({ src, url: next })
    })

    return () => {
      active = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [src])

  if (!result || result.src !== src) return null
  if (!result.url) return <>{fallback}</>
  return (
    <img
      src={result.url}
      alt={alt}
      className="mx-auto max-h-64 w-auto max-w-full rounded-lg object-contain"
    />
  )
}
