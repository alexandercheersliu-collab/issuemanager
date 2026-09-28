/** 多张照片 → 单页一图的 PDF（整卷导入前置步骤）。
 *
 * 手机照片普遍 3-8MB，直接拼 PDF 会超过网关 50MB 上限且上传慢；
 * 统一用 canvas 重编码：最长边压到 1600px、JPEG 0.8（AI 切题识别足够）。
 * jspdf 体积大，动态导入按需加载（仅整卷导入时下载）。
 */
const MAX_EDGE = 1600
const JPEG_QUALITY = 0.8
/** A4 尺寸（mm），jspdf 默认单位 */
const PAGE_W = 210
const PAGE_H = 297

function loadImage(file: File): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      URL.revokeObjectURL(url)
      resolve(img)
    }
    img.onerror = () => {
      URL.revokeObjectURL(url)
      reject(
        new Error(
          `「${file.name}」无法识别。iPhone 请把相机格式改为「兼容性最佳」` +
            '（设置 → 相机 → 格式），或改用相册里已转换的 JPEG 照片',
        ),
      )
    }
    img.src = url
  })
}

function toJpegDataUrl(img: HTMLImageElement): { dataUrl: string; width: number; height: number } {
  const scale = Math.min(1, MAX_EDGE / Math.max(img.naturalWidth, img.naturalHeight))
  const width = Math.round(img.naturalWidth * scale)
  const height = Math.round(img.naturalHeight * scale)
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('当前浏览器不支持图片处理')
  ctx.fillStyle = '#ffffff' // JPEG 无透明通道，垫底防黑底
  ctx.fillRect(0, 0, width, height)
  ctx.drawImage(img, 0, 0, width, height)
  return { dataUrl: canvas.toDataURL('image/jpeg', JPEG_QUALITY), width, height }
}

/** 把多张图片合成一个 PDF File：一图一页，按 A4 等比适配居中。 */
export async function imagesToPdf(files: File[], filename = 'scan.pdf'): Promise<File> {
  const { jsPDF } = await import('jspdf')
  const doc = new jsPDF({ unit: 'mm', format: 'a4' })
  for (let i = 0; i < files.length; i++) {
    const img = await loadImage(files[i])
    const { dataUrl, width, height } = toJpegDataUrl(img)
    const ratio = Math.min(PAGE_W / width, PAGE_H / height)
    const w = width * ratio
    const h = height * ratio
    if (i > 0) doc.addPage()
    doc.addImage(dataUrl, 'JPEG', (PAGE_W - w) / 2, (PAGE_H - h) / 2, w, h)
  }
  const blob = doc.output('blob')
  return new File([blob], filename, { type: 'application/pdf' })
}
