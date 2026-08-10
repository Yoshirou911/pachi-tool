const SEGMENT_DIGITS = new Map([
  ['1111110', '0'], ['0110000', '1'], ['1101101', '2'], ['1111001', '3'], ['0110011', '4'],
  ['1011011', '5'], ['1011111', '6'], ['1110000', '7'], ['1111111', '8'], ['1111011', '9'],
]);

function otsuThreshold(gray) {
  const histogram = new Uint32Array(256);
  for (const value of gray) histogram[value] += 1;
  let totalSum = 0;
  for (let i = 0; i < 256; i += 1) totalSum += i * histogram[i];
  let backgroundWeight = 0;
  let backgroundSum = 0;
  let bestVariance = -1;
  let threshold = 127;
  for (let i = 0; i < 256; i += 1) {
    backgroundWeight += histogram[i];
    if (!backgroundWeight) continue;
    const foregroundWeight = gray.length - backgroundWeight;
    if (!foregroundWeight) break;
    backgroundSum += i * histogram[i];
    const meanBackground = backgroundSum / backgroundWeight;
    const meanForeground = (totalSum - backgroundSum) / foregroundWeight;
    const variance = backgroundWeight * foregroundWeight * (meanBackground - meanForeground) ** 2;
    if (variance > bestVariance) {
      bestVariance = variance;
      threshold = i;
    }
  }
  return threshold;
}

function regionRatio(bits, width, height, left, top, right, bottom) {
  const x1 = Math.max(0, Math.floor(left * width));
  const x2 = Math.min(width, Math.ceil(right * width));
  const y1 = Math.max(0, Math.floor(top * height));
  const y2 = Math.min(height, Math.ceil(bottom * height));
  let on = 0;
  let total = 0;
  for (let y = y1; y < y2; y += 1) {
    for (let x = x1; x < x2; x += 1) {
      on += bits[y * width + x];
      total += 1;
    }
  }
  return total ? on / total : 0;
}

function recognizeSegmentDigit(bits, width, height) {
  const regions = [
    [.20, .02, .80, .13], [.76, .14, .98, .40], [.76, .60, .98, .86],
    [.20, .87, .80, .99], [.02, .60, .24, .86], [.02, .14, .24, .40], [.20, .45, .80, .57],
  ];
  const ratios = regions.map(region => regionRatio(bits, width, height, ...region));
  const pattern = ratios.map(value => value >= .22 ? '1' : '0').join('');
  const digit = SEGMENT_DIGITS.get(pattern) || '';
  const confidence = ratios.reduce((sum, value) => sum + Math.abs(value - .22), 0) / ratios.length;
  return { digit, confidence };
}

function segmentRuns(bits, width, height) {
  const active = new Array(width).fill(false);
  for (let x = 0; x < width; x += 1) {
    let count = 0;
    for (let y = 0; y < height; y += 1) count += bits[y * width + x];
    active[x] = count >= Math.max(2, height * .035);
  }
  const runs = [];
  let start = -1;
  for (let x = 0; x <= width; x += 1) {
    if (x < width && active[x] && start < 0) start = x;
    if ((x === width || !active[x]) && start >= 0) {
      if (x - start >= Math.max(2, width * .012)) runs.push([start, x]);
      start = -1;
    }
  }
  return runs;
}

function sevenSegmentPass(imageData, brightOnDark) {
  const { width, height, data } = imageData;
  const gray = new Uint8Array(width * height);
  for (let index = 0; index < gray.length; index += 1) {
    const offset = index * 4;
    gray[index] = Math.round(data[offset] * .299 + data[offset + 1] * .587 + data[offset + 2] * .114);
  }
  const threshold = otsuThreshold(gray);
  const bits = new Uint8Array(gray.length);
  for (let index = 0; index < gray.length; index += 1) {
    bits[index] = brightOnDark ? Number(gray[index] > threshold) : Number(gray[index] < threshold);
  }
  const foregroundRatio = bits.reduce((sum, value) => sum + value, 0) / bits.length;
  if (foregroundRatio < .01 || foregroundRatio > .65) return { text: '', confidence: 0, method: 'seven-segment' };
  const runs = segmentRuns(bits, width, height);
  const found = [];
  for (const [left, right] of runs.slice(0, 8)) {
    let top = height;
    let bottom = -1;
    for (let y = 0; y < height; y += 1) {
      for (let x = left; x < right; x += 1) {
        if (bits[y * width + x]) { top = Math.min(top, y); bottom = Math.max(bottom, y); }
      }
    }
    if (bottom <= top || bottom - top < height * .18) continue;
    const digitWidth = right - left;
    const digitHeight = bottom - top + 1;
    const cropped = new Uint8Array(digitWidth * digitHeight);
    for (let y = 0; y < digitHeight; y += 1) {
      for (let x = 0; x < digitWidth; x += 1) cropped[y * digitWidth + x] = bits[(top + y) * width + left + x];
    }
    const result = recognizeSegmentDigit(cropped, digitWidth, digitHeight);
    if (result.digit) found.push(result);
  }
  return {
    text: found.map(item => item.digit).join(''),
    confidence: found.length ? found.reduce((sum, item) => sum + item.confidence, 0) / found.length : 0,
    method: 'seven-segment',
  };
}

export function recognizeSevenSegment(imageData) {
  const candidates = [sevenSegmentPass(imageData, true), sevenSegmentPass(imageData, false)]
    .filter(item => /^\d{1,6}$/.test(item.text));
  return candidates.sort((a, b) => b.text.length - a.text.length || b.confidence - a.confidence)[0]
    || { text: '', confidence: 0, method: 'seven-segment' };
}

export async function recognizeNumberFromFile(file) {
  if (!file) throw new Error('画像を選んでください');
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 1200 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const context = canvas.getContext('2d', { willReadFrequently: true });
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();

  if ('TextDetector' in globalThis) {
    try {
      const blocks = await new globalThis.TextDetector().detect(canvas);
      const numbers = blocks.flatMap(block => String(block.rawValue || '').match(/\d{1,6}/g) || []);
      if (numbers.length) {
        const text = numbers.sort((a, b) => b.length - a.length)[0];
        return { value: Number(text), text, confidence: 1, method: 'text-detector', preview_url: canvas.toDataURL('image/jpeg', .78) };
      }
    } catch (_) { /* seven-segment fallback below */ }
  }

  const result = recognizeSevenSegment(context.getImageData(0, 0, canvas.width, canvas.height));
  if (!result.text) throw new Error('数字を読めませんでした。表示を大きく正面から撮り、手入力で確認してください');
  return { value: Number(result.text), ...result, preview_url: canvas.toDataURL('image/jpeg', .78) };
}
