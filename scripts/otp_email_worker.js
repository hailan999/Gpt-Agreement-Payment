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

    // OpenAI 邮件 HTML 里大量出现 #353740 / #10A37F 等品牌色 hex，fallback
    // \b\d{6}\b 会把全数字 hex（如 #353740）误抽成 OTP。
    // 用 negative lookbehind 排除前面是 # 的，并显式排除常见 CSS hex 上下文。
    const isHexColor = (haystack, idx) => {
      if (idx > 0 && haystack[idx - 1] === '#') return true;
      // "color:353740" / "background-color: #353740" / "bgcolor=\"353740\""
      const before = haystack.slice(Math.max(0, idx - 30), idx);
      return /(?:color|background|bgcolor|fill|stroke)\s*[:=]\s*["']?#?\s*$/i.test(before);
    };

    // OTP extraction — semantic context first to avoid grabbing tracking ids,
    // and skip any candidate that's a substring of the address digits.
    let otp = null;
    const candidates = [
      // "code is 123456", "verification code: 123456", etc.
      /(?:code(?:\s*is)?|verification|one[-\s]*time|verify|验证码)[^\d]{0,40}(\d{6})\b/gi,
      // ChatGPT subject template: "Your ChatGPT code is 123456"
      /chatgpt[^\d]{0,40}(\d{6})/gi,
      /openai[^\d]{0,40}(\d{6})/gi,
    ];
    const haystack = subject + '\n' + raw;
    for (const re of candidates) {
      let m;
      while ((m = re.exec(haystack)) !== null) {
        if (!isFromAddr(m[1]) && !isHexColor(haystack, m.index + m[0].lastIndexOf(m[1]))) {
          otp = m[1]; break;
        }
      }
      if (otp) break;
    }
    if (!otp) {
      // Body-only fallback: skip header section (从第一个空行后开始) so
      // To:/From:/Delivered-To: 里的数字不参与 fallback 匹配
      const bodyStart = raw.search(/\r?\n\r?\n/);
      const body = bodyStart >= 0 ? raw.slice(bodyStart) : raw;
      const re = /\b(\d{6})\b/g;
      let m;
      while ((m = re.exec(body)) !== null) {
        const cand = m[1];
        if (isFromAddr(cand)) continue;
        if (isHexColor(body, m.index)) continue;
        otp = cand; break;
      }
    }

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
