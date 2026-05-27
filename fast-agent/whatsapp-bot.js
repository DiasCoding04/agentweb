const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');

// Cấu hình cổng kết nối tới server.js
const PORT = process.env.FAST_AGENT_PORT || 18792;
const HOST = "127.0.0.1";

console.log("Đang khởi tạo WhatsApp Client...");

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
        args: ['--no-sandbox', '--disable-setuid-sandbox']
    }
});

client.on('qr', (qr) => {
    // Quét mã QR này bằng WhatsApp trên điện thoại của bạn
    qrcode.generate(qr, {small: true});
    console.log("\n=======================================================");
    console.log("Vui lòng quét mã QR trên bằng ứng dụng WhatsApp của bạn.");
    console.log("1. Mở WhatsApp trên điện thoại");
    console.log("2. Nhấn vào Menu (3 chấm) hoặc Cài đặt (Settings)");
    console.log("3. Chọn Thiết bị liên kết (Linked Devices)");
    console.log("4. Quét mã QR ở trên");
    console.log("=======================================================\n");
});

client.on('ready', () => {
    console.log('✅ WhatsApp Bot đã sẵn sàng và kết nối thành công!');
    console.log(`📡 Đang lắng nghe tin nhắn và chuyển tiếp tới http://${HOST}:${PORT}/api/chat`);
});

client.on('message_create', async msg => {
    // Bỏ qua các tin nhắn trạng thái (status/story)
    if (msg.from === 'status@broadcast') return;

    // CÁCH 1: CHỈ NHẬN TIN NHẮN TỪ CHÍNH MÌNH (Message Yourself)
    // Nếu bạn chat vào mục "Message yourself", tính năng của WhatsApp thường đặt msg.to là chính số của bạn.
    // Lọc: Chỉ xử lý nếu tin nhắn này do chính bạn gửi (msg.fromMe) VÀ người nhận cũng chính là bạn (hoặc client.info.wid).
    const myNumber = client.info.wid._serialized;
    if (!msg.fromMe || (msg.to !== myNumber && msg.from !== myNumber)) return;

    console.log(`\n[WhatsApp] Đã bắt được tin nhắn "Message yourself": ${msg.body}`);

    // Hiển thị trạng thái "đang gõ..." trên WhatsApp cho người dùng biết bot đang xử lý
    const chat = await msg.getChat();
    chat.sendStateTyping();

    try {
        const response = await fetch(`http://${HOST}:${PORT}/api/chat`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                sessionId: msg.from, // Dùng số điện thoại/ID của người gửi làm phiên làm việc
                message: msg.body,
                model: "gemini-3.1-flash-lite" // Yêu cầu sử dụng đúng model
            })
        });

        const data = await response.json();
        
        chat.clearState();

        if (data.ok && data.final) {
            console.log(`[Agent] Trả lời: ${data.final}`);
            msg.reply(data.final);
        } else if (data.error) {
            console.error(`[Agent Error]: ${data.error}`);
            msg.reply(`❌ Lỗi từ hệ thống: ${data.error}`);
        } else {
            console.log(`[Agent] Xong nhưng không có thông báo final.`);
            msg.reply(`✅ Đã xử lý xong tác vụ.`);
        }
    } catch (error) {
        chat.clearState();
        console.error("Lỗi khi gọi API /api/chat:", error);
        msg.reply("❌ Lỗi kết nối đến máy chủ Agent. Vui lòng kiểm tra xem server.js đã được bật chưa.");
    }
});

// Xử lý khi bị ngắt kết nối
client.on('disconnected', (reason) => {
    console.log('❌ WhatsApp đã bị ngắt kết nối:', reason);
    console.log('Đang thử kết nối lại...');
    client.initialize();
});

client.initialize();
