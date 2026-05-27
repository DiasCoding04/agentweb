const fs = require('fs');
let code = fs.readFileSync('server.js', 'utf8');
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
- Nếu một thao tác báo lỗi "element not found", hãy yêu cầu \`snapshot\` hoặc \`elements\` để lấy pid của giao diện hiện tại thay vì thử đoán bừa selector.
- Không có giới hạn bước cố định; hãy tiếp tục hành động khi còn chiến lược. Nếu bế tắc, trả \`final\` để hỏi người dùng.

SỔ TAY QUY TRÌNH ĐẶC BIỆT (PLAYBOOKS):
- Nếu URL chứa "ads.salontukawa.com" và bạn cần đếm chiến dịch hoặc lấy thông tin: (1) Nhìn cột bên trái, tìm \`pid\` của Tài Khoản mục tiêu và gọi lệnh click vào pid đó. (2) Chờ trang tải bảng điều khiển bên phải. (3) Dùng lệnh \`tables\` hoặc đếm thủ công các chiến dịch đang hoạt động. (4) Trả kết quả \`final\`.

VÍ DỤ HỢP LỆ (chỉ JSON):
1) Batch Lệnh (Khuyên dùng): {"tools": [{"cmd":"click","args":["--pid","10"]},{"cmd":"type","args":["--pid","12","--value","abc"]},{"cmd":"key","args":["--name","Enter"]}]}
2) Discovery: {"tools": [{"cmd":"elements","args":[]}]}
3) Goto: {"tools": [{"cmd":"goto","args":["https://example.com"]}]}
4) Final Báo cáo: {"final":"Đã xử lý xong tác vụ."}\`;`;

  code = code.slice(0, startIdx) + replacement + code.slice(endIdx);
  fs.writeFileSync('server.js', code);
  console.log('Successfully replaced system prompt.');
} else {
  console.log('Could not find boundaries.');
}
