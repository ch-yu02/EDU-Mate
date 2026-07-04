type MarkdownInlineNode =
  | {
      type: "text";
      text: string;
    }
  | {
      type: "strong";
      text: string;
    }
  | {
      type: "code";
      text: string;
    };

type MarkdownBlock =
  | {
      type: "heading";
      level: 3 | 4 | 5;
      text: string;
    }
  | {
      type: "paragraph";
      text: string;
    }
  | {
      type: "unordered-list" | "ordered-list";
      items: string[];
    }
  | {
      type: "quote";
      text: string;
    }
  | {
      type: "code";
      text: string;
      language?: string;
    };

type MarkdownContentProps = {
  className?: string;
  content: string;
};

export function MarkdownContent({ className = "agent-markdown", content }: MarkdownContentProps) {
  const blocks = parseMarkdownBlocks(content);
  if (blocks.length === 0) {
    return null;
  }

  return (
    <div className={className}>
      {blocks.map((block, index) => (
        <MarkdownBlockView block={block} key={index} />
      ))}
    </div>
  );
}

function MarkdownBlockView({ block }: { block: MarkdownBlock }) {
  if (block.type === "heading") {
    const HeadingTag = `h${block.level}` as "h3" | "h4" | "h5";
    return (
      <HeadingTag>
        <MarkdownInline content={block.text} />
      </HeadingTag>
    );
  }

  if (block.type === "paragraph") {
    return (
      <p>
        <MarkdownInline content={block.text} />
      </p>
    );
  }

  if (block.type === "unordered-list") {
    return (
      <ul>
        {block.items.map((item, index) => (
          <li key={index}>
            <MarkdownInline content={item} />
          </li>
        ))}
      </ul>
    );
  }

  if (block.type === "ordered-list") {
    return (
      <ol>
        {block.items.map((item, index) => (
          <li key={index}>
            <MarkdownInline content={item} />
          </li>
        ))}
      </ol>
    );
  }

  if (block.type === "quote") {
    return (
      <blockquote>
        <MarkdownInline content={block.text} />
      </blockquote>
    );
  }

  if (block.type === "code") {
    return (
      <pre>
        <code>{block.text}</code>
      </pre>
    );
  }

  return null;
}

function MarkdownInline({ content }: { content: string }) {
  const nodes = parseMarkdownInline(content);
  return (
    <>
      {nodes.map((node, index) => {
        if (node.type === "strong") {
          return <strong key={index}>{renderInlineText(node.text, index)}</strong>;
        }
        if (node.type === "code") {
          return <code key={index}>{node.text}</code>;
        }
        return <span key={index}>{renderInlineText(node.text, index)}</span>;
      })}
    </>
  );
}

function renderInlineText(text: string, keyPrefix: number) {
  const parts = text.split("\n");
  if (parts.length === 1) {
    return text;
  }

  return parts.flatMap((part, index) =>
    index === 0
      ? [part]
      : [<br key={`${keyPrefix}-${index}`} />, part],
  );
}

function parseMarkdownBlocks(content: string): MarkdownBlock[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: MarkdownBlock[] = [];
  let paragraph: string[] = [];
  let listType: "unordered-list" | "ordered-list" | null = null;
  let listItems: string[] = [];
  let codeLanguage: string | undefined;
  let codeLines: string[] | null = null;

  const flushParagraph = () => {
    const text = paragraph.join("\n").trim();
    if (text) {
      blocks.push({ type: "paragraph", text });
    }
    paragraph = [];
  };
  const flushList = () => {
    if (listType && listItems.length) {
      blocks.push({ type: listType, items: listItems });
    }
    listType = null;
    listItems = [];
  };
  const flushOpenTextBlocks = () => {
    flushParagraph();
    flushList();
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    const fenceMatch = line.match(/^```([A-Za-z0-9_-]+)?\s*$/);
    if (fenceMatch) {
      if (codeLines) {
        blocks.push({
          type: "code",
          text: codeLines.join("\n"),
          language: codeLanguage,
        });
        codeLines = null;
        codeLanguage = undefined;
      } else {
        flushOpenTextBlocks();
        codeLines = [];
        codeLanguage = fenceMatch[1];
      }
      continue;
    }

    if (codeLines) {
      codeLines.push(rawLine);
      continue;
    }

    if (!line.trim()) {
      flushOpenTextBlocks();
      continue;
    }

    const headingMatch = line.match(/^(#{1,3})\s+(.+)$/);
    if (headingMatch) {
      flushOpenTextBlocks();
      blocks.push({
        type: "heading",
        level: (headingMatch[1].length + 2) as 3 | 4 | 5,
        text: headingMatch[2].trim(),
      });
      continue;
    }

    const unorderedMatch = line.match(/^[-*]\s+(.+)$/);
    if (unorderedMatch) {
      flushParagraph();
      if (listType !== "unordered-list") {
        flushList();
        listType = "unordered-list";
      }
      listItems.push(unorderedMatch[1].trim());
      continue;
    }

    const orderedMatch = line.match(/^\d+[.)]\s+(.+)$/);
    if (orderedMatch) {
      flushParagraph();
      if (listType !== "ordered-list") {
        flushList();
        listType = "ordered-list";
      }
      listItems.push(orderedMatch[1].trim());
      continue;
    }

    const quoteMatch = line.match(/^>\s?(.+)$/);
    if (quoteMatch) {
      flushOpenTextBlocks();
      blocks.push({
        type: "quote",
        text: quoteMatch[1].trim(),
      });
      continue;
    }

    flushList();
    paragraph.push(line);
  }

  if (codeLines) {
    blocks.push({
      type: "code",
      text: codeLines.join("\n"),
      language: codeLanguage,
    });
  }
  flushOpenTextBlocks();

  return blocks;
}

function parseMarkdownInline(content: string): MarkdownInlineNode[] {
  const nodes: MarkdownInlineNode[] = [];
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
  let lastIndex = 0;
  for (const match of content.matchAll(pattern)) {
    if (match.index > lastIndex) {
      nodes.push({
        type: "text",
        text: content.slice(lastIndex, match.index),
      });
    }

    const token = match[0];
    if (token.startsWith("`")) {
      nodes.push({
        type: "code",
        text: token.slice(1, -1),
      });
    } else {
      nodes.push({
        type: "strong",
        text: token.slice(2, -2),
      });
    }
    lastIndex = match.index + token.length;
  }

  if (lastIndex < content.length) {
    nodes.push({
      type: "text",
      text: content.slice(lastIndex),
    });
  }

  return nodes;
}
