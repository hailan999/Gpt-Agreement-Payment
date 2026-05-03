// Cloudflare Email Worker — receives mail via Email Routing catch-all,
// extracts a 6-digit OTP, stores it in KV keyed by recipient address.
//
// Bindings (set by setup_cf_email_worker.py):
//   OTP_KV       — KV namespace for {recipient → {otp, ts, from, subject}}
//   FALLBACK_TO  — (optional) plain_text. If set, forward raw email to this
//                  address as well (useful during migration off IMAP/QQ).
//
// Pipeline reads KV via CF API (CTF-reg/cf_kv_otp_provider.py).

export default {
  async email(message, env, ctx) {
    const to = (message.to || '').toLowerCase();
    const from = message.from || '';

    // Read the raw RFC822 message into a string
    let raw = '';
    try {
      const reader = message.raw.getReader();
      const decoder = new TextDecoder('utf-8', { fatal: false });
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        raw += decoder.decode(value, { stream: true });
      }
      raw += decoder.decode();
    } catch (e) {
      console.error('raw read failed:', e && e.message);
    }

    // Pull the Subject header out for fast-path matching. RFC822 headers may
    // be folded across multiple lines, so unfold continuation lines first.
    const headerEnd = raw.search(/\r?\n\r?\n/);
    const headers = headerEnd >= 0 ? raw.slice(0, headerEnd) : raw;
    const unfoldedHeaders = headers.replace(/\r?\n[ \t]+/g, ' ');
    const subjMatch = unfoldedHeaders.match(/^Subject:\s*(.+)$/im);
    const subject = subjMatch ? decodeMimeWords(subjMatch[1]).trim().slice(0, 200) : '';

    // 收件地址 + 发件地址里的数字（zone 名常含 6 位，会被 fallback regex
    // 误抽成 OTP，比如 random@123456.example.com 这种 zone → "123456" 假阳性）
    const addrDigits = ((to + ' ' + from).match(/\d/g) || []).join('');
    const isFromAddr = (s) => addrDigits.length >= 6 && addrDigits.includes(s);

    const extraction = extractOtp({ raw, subject, isFromAddr });
    const otp = extraction.otp;

    if (otp && to) {
      const payload = JSON.stringify({
        otp,
        ts: Date.now(),
        from,
        subject,
        source: extraction.source,
      });
      try {
        await env.OTP_KV.put(to, payload, { expirationTtl: 600 });
        console.log(`stored OTP for ${to.slice(0, 40)} source=${extraction.source} (subject="${subject.slice(0, 60)}")`);
      } catch (e) {
        console.error('KV put failed:', e && e.message);
      }
    } else {
      console.log(`no OTP extracted to=${to.slice(0, 40)} subject="${subject.slice(0, 60)}"`);
    }

    // Optional: forward raw email to fallback mailbox (e.g. existing QQ inbox)
    // Useful during the IMAP→KV migration to keep both paths warm.
    if (env.FALLBACK_TO) {
      try {
        await message.forward(env.FALLBACK_TO);
      } catch (e) {
        console.error('forward failed:', e && e.message);
      }
    }
  },
};

function extractOtp({ raw, subject, isFromAddr }) {
  const bodyStart = raw.search(/\r?\n\r?\n/);
  const body = bodyStart >= 0 ? raw.slice(bodyStart) : raw;
  const decodedBody = decodeQuotedPrintable(body);
  const visibleText = htmlToText(decodedBody);
  const haystacks = [
    ['subject+visible', `${subject}\n${visibleText}`],
    ['visible', visibleText],
    ['decoded-body', decodedBody],
  ];

  for (const [source, text] of haystacks) {
    const otp = findSemanticOtp(text, isFromAddr);
    if (otp) return { otp, source };
  }

  const subjectLooksRight = /chatgpt|openai|verification|temporary|code/i.test(subject);
  if (subjectLooksRight) {
    const all = visibleText.match(/\b\d{6}\b/g) || [];
    for (const cand of all) {
      if (!isFromAddr(cand)) return { otp: cand, source: 'visible-fallback' };
    }
  }

  return { otp: null, source: 'none' };
}

function findSemanticOtp(text, isFromAddr) {
  const patterns = [
    /(?:code(?:\s*is)?|verification\s*code|temporary\s*code|one[-\s]*time\s*code|验证码)[\s\S]{0,160}?([0-9][0-9\s-]{4,20}[0-9])\b/gi,
    /([0-9][0-9\s-]{4,20}[0-9])[\s\S]{0,80}?(?:is\s+your|your)\s+(?:chatgpt|openai)?\s*(?:verification\s*)?code/gi,
    /(?:chatgpt|openai)[\s\S]{0,180}?([0-9][0-9\s-]{4,20}[0-9])\b/gi,
  ];
  for (const re of patterns) {
    let m;
    while ((m = re.exec(text || '')) !== null) {
      const otp = normalizeOtp(m[1]);
      if (otp && !isFromAddr(otp)) return otp;
    }
  }
  return null;
}

function normalizeOtp(s) {
  const digits = String(s || '').replace(/\D/g, '');
  return digits.length === 6 ? digits : null;
}

function htmlToText(s) {
  return String(s || '')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&#(\d+);/g, (_m, n) => String.fromCharCode(Number(n)))
    .replace(/&#x([0-9a-f]+);/gi, (_m, n) => String.fromCharCode(parseInt(n, 16)))
    .replace(/\s+/g, ' ')
    .trim();
}

function decodeQuotedPrintable(s) {
  const text = String(s || '').replace(/=\r?\n/g, '');
  const bytes = [];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '=' && /^[0-9A-Fa-f]{2}$/.test(text.slice(i + 1, i + 3))) {
      bytes.push(parseInt(text.slice(i + 1, i + 3), 16));
      i += 2;
    } else {
      bytes.push(text.charCodeAt(i) & 0xff);
    }
  }
  try {
    return new TextDecoder('utf-8', { fatal: false }).decode(new Uint8Array(bytes));
  } catch (_e) {
    return text;
  }
}

function decodeMimeWords(s) {
  return String(s || '').replace(/=\?([^?]+)\?([bqBQ])\?([^?]*)\?=/g, (_m, charset, enc, text) => {
    const label = String(charset || 'utf-8').toLowerCase();
    if (label !== 'utf-8' && label !== 'us-ascii') return text;
    try {
      if (String(enc).toUpperCase() === 'B') {
        const bin = atob(text);
        const bytes = Uint8Array.from(bin, c => c.charCodeAt(0));
        return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
      }
      const qp = text.replace(/_/g, ' ').replace(/=([0-9A-Fa-f]{2})/g, (_h, x) =>
        String.fromCharCode(parseInt(x, 16))
      );
      const bytes = Uint8Array.from(qp, c => c.charCodeAt(0));
      return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
    } catch (_e) {
      return text;
    }
  });
}
