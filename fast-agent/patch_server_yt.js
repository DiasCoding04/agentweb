const fs = require('fs');
let code = fs.readFileSync('server.js', 'utf8');

// 1. Add evaluate tool definition
const toolDefsStart = code.indexOf('const BROWSER_TOOLS = {');
if (toolDefsStart > -1 && code.indexOf('"evaluate": {') === -1) {
  const insertPos = code.indexOf('  "snapshot": {', toolDefsStart);
  if (insertPos > -1) {
    const evaluateToolDef = `  "evaluate": {
    "desc": "Chạy một đoạn mã Javascript thuần túy (client-side) trên trang web hiện tại và trả về kết quả. Rất hữu ích để lấy href, text hoặc DOM phức tạp.",
    "args": {
      "--expr": "Đoạn mã Javascript (ví dụ: \\"Array.from(document.querySelectorAll('a')).map(a => a.href)\\")"
    }
  },
`;
    code = code.slice(0, insertPos) + evaluateToolDef + code.slice(insertPos);
  }
}

// 2. Add YouTube Playbook to System Prompt using single quotes
const playbookStart = code.indexOf('SỔ TAY QUY TRÌNH ĐẶC BIỆT (PLAYBOOKS):');
if (playbookStart > -1) {
  const newPlaybook = `SỔ TAY QUY TRÌNH ĐẶC BIỆT (PLAYBOOKS):
- Nếu tác vụ yêu cầu Mở nhạc/Video YouTube (VD: "mở bài Vinh Khuất", "bật nhạc Sơn Tùng"): 
  (1) Không bao giờ dùng Tab+Enter mù quáng. 
  (2) Dùng lệnh 'goto' để tìm kiếm: '{"cmd":"goto","args":["https://www.youtube.com/results?search_query=vinh+khuat"]}'
  (3) Dùng 'evaluate' để lấy link video đầu tiên: '{"cmd":"evaluate","args":["--expr","Array.from(document.querySelectorAll(\\'ytd-video-renderer a#video-title\\')).slice(0,3).map(a => ({title: a.textContent.trim(), href: a.href}))"]}'
  (4) Dùng 'goto' để mở link video vừa lấy được.
- Nếu URL chứa "ads.salontukawa.com" và bạn cần đếm chiến dịch hoặc lấy thông tin: (1) Nhìn cột bên trái, tìm 'pid' của Tài Khoản mục tiêu và gọi lệnh click vào pid đó. (2) Chờ trang tải bảng điều khiển bên phải. (3) Dùng lệnh 'tables' hoặc đếm thủ công các chiến dịch đang hoạt động. (4) Trả kết quả 'final'.`;
  
  const playbookEnd = code.indexOf('VÍ DỤ HỢP LỆ (chỉ JSON):', playbookStart);
  code = code.slice(0, playbookStart) + newPlaybook + '\n\n' + code.slice(playbookEnd);
}

// 3. Fix SAFETY_BLOCK handling
const safetyBlockStart = code.indexOf('const unsafeReason = needsSaferTarget(toolCmd, toolArgs);');
if (safetyBlockStart > -1) {
  // Find the exact end of the block which is 'continue;\n    }'
  const safetyBlockEnd = code.indexOf('continue;\n    }', safetyBlockStart) + 'continue;\n    }'.length;
  if (safetyBlockEnd > safetyBlockStart) {
    const newSafetyBlock = `const unsafeReason = needsSaferTarget(toolCmd, toolArgs);
    if (unsafeReason) {
      history.push({ role: "model", text: JSON.stringify(model.parsed) });
      history.push({ role: "user", text: \`SAFETY_BLOCK \${toolCmd}: \${unsafeReason}.\` });
      await runObservation(trace, history, "SAFETY_BLOCK_OBSERVATION", { includeHtml: true });
      nextInputs.push(\`Hành động '\${toolCmd}' bị chặn an toàn do: \${unsafeReason}. GỢI Ý: Nếu bạn đang cố click mù, hãy thử dùng goto() với URL trực tiếp, hoặc dùng evaluate() để lấy chính xác href thay vì dò dẫm bằng phím Tab.\`);
      continue;
    }`;
    code = code.slice(0, safetyBlockStart) + newSafetyBlock + code.slice(safetyBlockEnd);
  }
}

fs.writeFileSync('server.js', code);
console.log('Server.js patched successfully for YouTube features.');
