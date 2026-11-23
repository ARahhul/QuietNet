/** Long edge in pixels: plenty for an AI to read small menu text, small enough to upload quickly. */
const MAX_EDGE = 1800

/** Any picture -> a JPEG data URL no larger than MAX_EDGE. Also drops EXIF (phone photos carry location). */
export async function shrink(file: Blob): Promise<string> {
  const bmp = await createImageBitmap(file)
  const scale = Math.min(1, MAX_EDGE / Math.max(bmp.width, bmp.height))
  const w = Math.max(1, Math.round(bmp.width * scale))
  const h = Math.max(1, Math.round(bmp.height * scale))
  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d')!
  ctx.fillStyle = '#fff' // transparent PNGs would otherwise turn black
  ctx.fillRect(0, 0, w, h)
  ctx.drawImage(bmp, 0, 0, w, h)
  bmp.close()
  return canvas.toDataURL('image/jpeg', 0.85)
}
