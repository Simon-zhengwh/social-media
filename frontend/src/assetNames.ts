const IMAGE_EXTENSION = /\.(?:jpe?g|png|webp|gif|heic|heif|avif|bmp|tiff?|svg)$/i

export function assetDisplayName(fileName: string): string {
  return fileName.replace(IMAGE_EXTENSION, '') || fileName
}

export function assetRenameFileName(name: string, originalFileName: string): string {
  return assetDisplayName(name) + (originalFileName.match(IMAGE_EXTENSION)?.[0] ?? '')
}
