(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PimcampMailText = api;
})(globalThis, function () {
  "use strict";

  const namedEntities = {
    amp: "&", apos: "'", bull: "•", copy: "©", gt: ">", hellip: "…",
    ldquo: "“", lsquo: "‘", lt: "<", mdash: "—", nbsp: "\u00a0", ndash: "–",
    quot: '"', reg: "®", rdquo: "”", rsquo: "’", trade: "™"
  };
  const droppedContent = new Set(["script", "style", "form", "iframe", "object", "embed", "svg", "math", "template", "noscript"]);
  const voidTags = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]);
  const blockTags = new Set(["address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "ol", "p", "pre", "section", "table", "tr", "ul"]);

  function decodeEntities(value) {
    return value.replace(/&(#(?:x[0-9a-f]+|[0-9]+)|[a-z][a-z0-9]+);/gi, (entity, token) => {
      if (token[0] !== "#") return namedEntities[token.toLowerCase()] ?? entity;
      const hex = token[1]?.toLowerCase() === "x";
      const digits = token.slice(hex ? 2 : 1);
      const point = Number.parseInt(digits, hex ? 16 : 10);
      if (!Number.isFinite(point) || point <= 0 || point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff)) return "\ufffd";
      return String.fromCodePoint(point);
    });
  }

  function endOfTag(html, start) {
    let quote = "";
    for (let index = start + 1; index < html.length; index += 1) {
      const character = html[index];
      if (quote) {
        if (character === quote) quote = "";
      } else if (character === '"' || character === "'") quote = character;
      else if (character === ">") return index + 1;
    }
    return html.length;
  }

  function normalizeText(value) {
    return value
      .replace(/\r\n?/g, "\n")
      .replace(/[\t\f\v ]+/g, " ")
      .split("\n")
      .map(line => line.trim())
      .join("\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  // Tokenizes markup without constructing a document or materializing resource URLs.
  function htmlToText(html) {
    const input = String(html ?? "");
    const output = [];
    const skipped = [];
    let index = 0;

    while (index < input.length) {
      if (input.startsWith("<!--", index)) {
        const end = input.indexOf("-->", index + 4);
        index = end < 0 ? input.length : end + 3;
        continue;
      }

      if (input.startsWith("<!", index) || input.startsWith("<?", index)) {
        index = endOfTag(input, index);
        continue;
      }

      if (input[index] === "<") {
        const match = /^<\s*(\/?)\s*([a-z][a-z0-9:-]*)\b/i.exec(input.slice(index));
        if (match) {
          const closing = Boolean(match[1]);
          const tag = match[2].toLowerCase();
          const end = endOfTag(input, index);
          const selfClosing = /\/\s*>$/.test(input.slice(index, end)) || voidTags.has(tag);

          if (skipped.length) {
            if (droppedContent.has(tag)) {
              if (closing) {
                const skipAt = skipped.lastIndexOf(tag);
                if (skipAt >= 0) skipped.splice(skipAt, 1);
              } else if (!selfClosing) skipped.push(tag);
            }
          } else if (droppedContent.has(tag)) {
            if (!closing && !selfClosing) skipped.push(tag);
          } else if (tag === "br" || tag === "hr") {
            output.push("\n");
          } else if (blockTags.has(tag)) {
            output.push("\n");
          } else if (tag === "td" || tag === "th") {
            output.push(" ");
          }
          index = end;
          continue;
        }
      }

      const next = input.indexOf("<", index + 1);
      const end = next < 0 ? input.length : next;
      if (!skipped.length) output.push(decodeEntities(input.slice(index, end)));
      index = end;
    }

    return normalizeText(output.join(""));
  }

  function sourceText(source) {
    if (typeof source?.body_text === "string") return source.body_text;
    const parts = Array.isArray(source?.body_parts) ? source.body_parts : [];
    const plain = parts.find(part => part?.media_type === "text/plain" && typeof part.content_utf8 === "string");
    if (plain) return plain.content_utf8;
    const html = parts.find(part => part?.media_type === "text/html" && typeof part.content_utf8 === "string");
    return html ? htmlToText(html.content_utf8) : "";
  }

  return { decodeEntities, htmlToText, normalizeText, sourceText };
});
