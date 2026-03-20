/**
 * FOIA.io — Word Document Letter Generator
 * Run: node generate_letter.js '<json>'
 * Or integrate as an API endpoint via child_process
 *
 * Usage:
 *   node generate_letter.js '{"foia_number":"FOIA-2026-001","agency_name":"FBI","officer_title":"FOIA Officer","subject":"All records...","output":"output.docx"}'
 */

const {
  Document, Packer, Paragraph, TextRun, AlignmentType,
  HeadingLevel, BorderStyle, WidthType
} = require('docx');
const fs = require('fs');

const args = JSON.parse(process.argv[2] || '{}');
const {
  foia_number   = 'FOIA-2026-001',
  agency_name   = 'Federal Agency',
  officer_title = 'FOIA Officer',
  subject       = '[Subject of Request]',
  requester_name  = '[Your Name]',
  requester_addr  = '[Your Address]',
  requester_email = '[Your Email]',
  requester_phone = '[Your Phone]',
  output        = 'foia_letter.docx'
} = args;

const today = new Date().toLocaleDateString('en-US', {
  year: 'numeric', month: 'long', day: 'numeric'
});

const FEE_WAIVER = `I request a waiver of all fees associated with this request. I am a member of the news media (or an independent journalist) and the requested records concern the operations or activities of the federal government. Disclosure of the requested information is likely to contribute significantly to public understanding of government operations and is not primarily in my commercial interest. See 5 U.S.C. § 552(a)(4)(A)(iii). If a fee waiver is not granted in full, I request that fees be limited to duplication costs only, and that I be notified before any charges exceeding $25.00 are incurred.`;

const CLOSING = `If my request is denied in whole or in part, I ask that you justify all deletions by reference to specific exemptions of the Act. I will appeal any improper withholding. I expect a response within twenty (20) business days as required by statute. If you have any questions, please contact me at the information below.`;

function p(text, opts = {}) {
  return new Paragraph({
    spacing: { after: opts.after ?? 200, before: opts.before ?? 0 },
    alignment: opts.align ?? AlignmentType.LEFT,
    children: [new TextRun({
      text,
      bold: opts.bold ?? false,
      italics: opts.italic ?? false,
      size: opts.size ?? 24,   // 12pt
      font: 'Times New Roman',
      color: opts.color ?? '000000',
    })]
  });
}

function gap() {
  return new Paragraph({ spacing: { after: 200 }, children: [new TextRun('')] });
}

const doc = new Document({
  styles: {
    default: {
      document: { run: { font: 'Times New Roman', size: 24 } }
    }
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1800 }
      }
    },
    children: [
      // Date
      p(today, { after: 400 }),

      // Officer title & agency
      p(officer_title, { bold: true }),
      p(agency_name),
      gap(),

      // Re line
      p(`Re: Freedom of Information Act Request — ${foia_number}`, { bold: true, after: 300 }),
      p(`Subject: ${subject}`, { italic: true, after: 400 }),

      // Salutation
      p(`Dear ${officer_title}:`, { after: 300 }),

      // Opening paragraph
      p(`Pursuant to the Freedom of Information Act, 5 U.S.C. § 552, and applicable agency regulations, I hereby request access to and copies of the following records:`, { after: 300 }),

      // Subject block
      new Paragraph({
        spacing: { before: 120, after: 300 },
        indent: { left: 720 },
        children: [new TextRun({
          text: subject,
          bold: true,
          size: 24,
          font: 'Times New Roman',
        })]
      }),

      // Elaboration placeholder
      p('[Please describe the records you are seeking in detail here. Include relevant dates, names of individuals, document types, programs, offices, or other identifying information that will help agency personnel locate the responsive records. The more specific you are, the faster your request will be processed.]', {
        color: '666666', italic: true, after: 400
      }),

      // Description of requester
      p(`I am an independent journalist/member of the news media. The records I am requesting concern the operations or activities of the federal government and are likely to contribute significantly to public understanding of those operations or activities.`, { after: 300 }),

      // Fee waiver section header
      p('FEE WAIVER REQUEST', { bold: true, after: 160 }),
      p(FEE_WAIVER, { after: 400 }),

      // Closing section
      p('ADDITIONAL PROVISIONS', { bold: true, after: 160 }),
      p(CLOSING, { after: 400 }),

      // Thank you
      p('Thank you for your attention to this matter.', { after: 400 }),

      // Closing
      p('Respectfully submitted,', { after: 600 }),

      p(requester_name, { bold: true }),
      p(requester_addr),
      p(requester_email),
      p(requester_phone),
    ]
  }]
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync(output, buf);
  console.log(JSON.stringify({ ok: true, output }));
}).catch(err => {
  console.error(JSON.stringify({ ok: false, error: err.message }));
  process.exit(1);
});
