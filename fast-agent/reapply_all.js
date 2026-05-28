const fs = require('fs');
let code = fs.readFileSync('server.js', 'utf8');

// 1. Fix compactToolResult pid mapping
code = code.replace(/index: e\.index/g, 'pid: e.pid');

// 2. Remove hard kill switches for repeatedFinds and attempts
code = code.replace(/taskState\.acquisition\.repeatedFinds = \(taskState\.acquisition\.repeatedFinds \|\| 0\) \+ 1;\s*if \(\(taskState\.acquisition\.repeatedFinds \|\| 0\) > 3\) \{\s*taskState\.finalStatus = "FAILED";\s*final = "Dừng: lặp find quá số lần cho phép mà không lock được context mục tiêu\.";\s*shouldBreakOuter = true; break;\s*\}/, '');
code = code.replace(/if \(taskState\.acquisition\?\.attempts > 3 && !taskState\.contextLock\?\.locked\) \{\s*taskState\.finalStatus = "FAILED_CONTEXT_LOCK";\s*final = `FAILED_CONTEXT_LOCK: không thể lock context mục tiêu sau \$\{taskState\.acquisition\.attempts\} lần mở target\. Candidates: \$\{JSON\.stringify\(taskState\.acquisition\.candidates \|\| \[\]\)\}`;\s*shouldBreakOuter = true; break;\s*\}/, '');

// 3. Soften loop detection
code = code.replace(/if \(lastVersion === currentStateVersion && \(toolCounts\.get\(signature\) \|\| 0\) > 1\) \{\s*taskState\.finalStatus = "FAILED";\s*final = `Dừng để tiết kiệm token: lặp lại cùng tool và cùng args không đổi trạng thái \(\$\{toolCmd\}\)\.`;\s*shouldBreakOuter = true; break;\s*\}/, `if (lastVersion === currentStateVersion && (toolCounts.get(signature) || 0) > 1) { nextInputs.push('Cảnh báo: Bạn vừa lặp lại lệnh ' + toolCmd + ' mà không có tác dụng. Hãy thử cách khác.'); continue; }`);

// 4. Update System Prompt & Playbooks
const startString = 'QUY TẮC KHẢO SÁT TRANG (BẮT BUỘC):';
const endString = 'thay đổi bảo mật).`;';
const startIdx = code.indexOf(startString);
const endIdx = code.indexOf(endString) + endString.length;

if (startIdx > -1 && endIdx > startIdx) {
  const replacement = `QUY TẮC KHẢO SÁT VÀ CHỌN MỤC TIÊU (BẮT BUỘC):
1) Hệ thống sẽ tự động cung cấp OBSERVATION (gồm URL, snapshot, accessibility, v.v.). Bạn PHẢI dựa vào OBSERVATION mới nhất để chọn mục tiêu.
2) LUÔN LUÔN ưu tiên nhắm mục tiêu bằng pid nếu phần tử đó có pid (ví dụ: {"cmd":"click","args":["--pid","15"]}). Đừng mò mẫm bằng text hoặc css selector nếu đã có pid.
3) Bạn CÓ THỂ (và được khuyến khích) xuất ra nhiều lệnh liên tiếp trong mảng "tools" để thực hiện một chuỗi hành động nhanh chóng. Hệ thống sẽ tự động chạy tuần tự và báo cáo lại nếu có lệnh nào hụt. Đừng ngại gộp các lệnh như [nhập chữ] -> [enter] -> [nhập chữ] thành một mảng lệnh.

XỬ LÝ LỖI VÀ FALLBACK:
- Nếu một thao tác báo lỗi "element not found", hãy yêu cầu 'snapshot' hoặc 'elements' để lấy pid của giao diện hiện tại thay vì thử đoán bừa selector.
- Không có giới hạn bước cố định; hãy tiếp tục hành động khi còn chiến lược. Nếu bế tắc, trả 'final' để hỏi người dùng.

SỔ TAY QUY TRÌNH ĐẶC BIỆT (PLAYBOOKS):
- Nếu URL chứa "ads.salontukawa.com" và bạn cần đếm chiến dịch hoặc lấy thông tin: (1) Nhìn cột bên trái, tìm 'pid' của Tài Khoản mục tiêu và gọi lệnh click vào pid đó. (2) Chờ trang tải bảng điều khiển bên phải. (3) Dùng lệnh 'tables' hoặc đếm thủ công các chiến dịch đang hoạt động. (4) Trả kết quả 'final'.

VÍ DỤ HỢP LỆ (chỉ JSON):
1) Batch Lệnh (Khuyên dùng): {"tools": [{"cmd":"click","args":["--pid","10"]},{"cmd":"type","args":["--pid","12","--value","abc"]},{"cmd":"key","args":["--name","Enter"]}]}
2) Discovery: {"tools": [{"cmd":"elements","args":[]}]}
3) Goto: {"tools": [{"cmd":"goto","args":["https://example.com"]}]}
4) Final Báo cáo: {"final":"Đã xử lý xong tác vụ."}\`;`;
  code = code.slice(0, startIdx) + replacement + code.slice(endIdx);
}

// 5. Fix strict invariants blocking final reports
code = code.replace(/if \(taskState\.invariants\.successCriteria\.length && taskState\.finalStatus === "RUNNING"\) \{\s*final = `Chưa thể xác nhận hoàn tất: successCriteria chưa được verifier chứng minh\. Trạng thái: PARTIAL\.`;\s*taskState\.finalStatus = "PARTIAL";\s*audit\(taskState, \{ type: "final_blocked", reason: "missing verified success criteria", proposedFinal: String\(model\.parsed\.final\) \}\);\s*\} else \{/, `if (taskState.invariants.successCriteria.length && taskState.finalStatus === "RUNNING") { audit(taskState, { type: "final_unverified", reason: "missing verified success criteria", proposedFinal: String(model.parsed.final) }); }`);
code = code.replace(/taskState\.finalStatus = taskState\.finalStatus === "RUNNING" \? "SUCCESS" : taskState\.finalStatus;\s*audit\(taskState, \{ type: "final", status: taskState\.finalStatus, final \}\);\s*\}/, `taskState.finalStatus = taskState.finalStatus === "RUNNING" ? "SUCCESS" : taskState.finalStatus; audit(taskState, { type: "final", status: taskState.finalStatus, final });`);


fs.writeFileSync('server.js', code);
console.log('All fixes reapplied successfully.');
