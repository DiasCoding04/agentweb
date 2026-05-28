import os

with open('server.js', 'r', encoding='utf-8') as f:
    code = f.read()

start_str = """    const tool = model.parsed.tool;
    if (!tool || !tool.cmd) throw new Error("JSON không có final hoặc tool.cmd.");"""

end_str = """    const shortResult = compactResult;
    const resultText = `Ket qua tool ${toolCmd} (${result.ms}ms, ok=${logicalOk}): ${shortResult}`;
    history.push({ role: "model", text: JSON.stringify(model.parsed) });
    history.push({ role: "user", text: resultText.slice(0, TOOL_RESULT_CHARS + 200) });
    nextInput = `Tiep tuc tu ket qua tool va OBSERVATION moi nhat trong lich su. Neu vua thao tac, hay kiem tra VERIFY_OBSERVATION de xac nhan dung trang thai truoc khi lam tiep. Neu xong thi final, neu can thao tac tiep thi goi tool tiep. Neu khong du chac chan thi hoi nguoi dung. Khong lap lai tool vua loi qua 2 lan.\\n${resultText.slice(0, TOOL_RESULT_CHARS + 200)}`;"""

start_idx = code.find(start_str)
end_idx = code.find(end_str)

if start_idx == -1 or end_idx == -1:
    print("Block not found")
    exit(1)

block_end = end_idx + len(end_str)
block = code[start_idx:block_end]

# 1. Replace the header
new_header = """    const tools = model.parsed.tools || (model.parsed.tool ? [model.parsed.tool] : []);
    if (!tools.length) throw new Error("JSON không có final hoặc tools.");

    history.push({ role: "model", text: JSON.stringify(model.parsed) });

    let nextInputs = [];
    let shouldBreakOuter = false;

    for (const tool of tools) {
      if (!tool || !tool.cmd) continue;"""
block = block.replace(start_str, new_header, 1)

# 2. Replace the tail
new_tail = """      const shortResult = compactResult;
      const resultText = `Ket qua tool ${toolCmd} (${result.ms}ms, ok=${logicalOk}): ${shortResult}`;
      history.push({ role: "user", text: resultText.slice(0, TOOL_RESULT_CHARS + 200) });
      nextInput = `Tiep tuc tu ket qua tool va OBSERVATION moi nhat trong lich su. Neu vua thao tac, hay kiem tra VERIFY_OBSERVATION de xac nhan dung trang thai truoc khi lam tiep. Neu xong thi final, neu can thao tac tiep thi goi tool tiep. Neu khong du chac chan thi hoi nguoi dung. Khong lap lai tool vua loi qua 2 lan.\\n${resultText.slice(0, TOOL_RESULT_CHARS + 200)}`;
      nextInputs.push(nextInput);
    }
    if (shouldBreakOuter) break;
    nextInput = nextInputs.join("\\n\\n");"""
block = block.replace(end_str, new_tail, 1)

# 3. Replace all break/continue inside the block (but avoid replacing the newly added ones)
lines = block.split('\\n')
for i, line in enumerate(lines):
    # Don't touch the new header or tail we just inserted
    if "shouldBreakOuter" in line or "nextInputs.push" in line:
        continue
    
    if line.strip() == "break;":
        # Check if it's inside the big switch/case (no switch/case here!)
        lines[i] = line.replace("break;", "shouldBreakOuter = true; break;")
    elif line.strip() == "continue;":
        lines[i] = line.replace("continue;", "nextInputs.push(nextInput); continue;")

new_block = '\\n'.join(lines)

new_code = code[:start_idx] + new_block + code[block_end:]
with open('server.js', 'w', encoding='utf-8') as f:
    f.write(new_code)

print("Refactored successfully")
