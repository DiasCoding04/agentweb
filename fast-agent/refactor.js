const fs = require('fs');

let code = fs.readFileSync('server.js', 'utf8');

const startPattern = /    const tool = model\.parsed\.tool;\s+if \(!tool \|\| !tool\.cmd\) throw new Error\("JSON không có final hoặc tool\.cmd\."\);/;
const endPattern = /    const shortResult = compactResult;\s+const resultText = `Ket qua tool \$\{toolCmd\} \(\$\{result\.ms\}ms, ok=\$\{logicalOk\}\): \$\{shortResult\}`;\s+history\.push\(\{ role: "model", text: JSON\.stringify\(model\.parsed\) \}\);\s+history\.push\(\{ role: "user", text: resultText\.slice\(0, TOOL_RESULT_CHARS \+ 200\) \}\);\s+nextInput = `Tiep tuc tu ket qua tool va OBSERVATION moi nhat trong lich su\. Neu vua thao tac, hay kiem tra VERIFY_OBSERVATION de xac nhan dung trang thai truoc khi lam tiep\. Neu xong thi final, neu can thao tac tiep thi goi tool tiep\. Neu khong du chac chan thi hoi nguoi dung\. Khong lap lai tool vua loi qua 2 lan\.\\n\$\{resultText\.slice\(0, TOOL_RESULT_CHARS \+ 200\)\}`;/;

const startMatch = code.match(startPattern);
const endMatch = code.match(endPattern);

if (!startMatch || !endMatch) {
    console.log("Could not find block boundaries via regex.");
    console.log("Start match:", !!startMatch);
    console.log("End match:", !!endMatch);
    process.exit(1);
}

const startIdx = startMatch.index;
const endIdx = endMatch.index;
const blockEnd = endIdx + endMatch[0].length;

let block = code.substring(startIdx, blockEnd);

const newHeader = `    const tools = model.parsed.tools || (model.parsed.tool ? [model.parsed.tool] : []);
    if (!tools.length) throw new Error("JSON không có final hoặc tools.");

    history.push({ role: "model", text: JSON.stringify(model.parsed) });

    let nextInputs = [];
    let shouldBreakOuter = false;

    for (const tool of tools) {
      if (!tool || !tool.cmd) continue;`;

block = block.replace(startPattern, newHeader);

const newTail = `      const shortResult = compactResult;
      const resultText = \`Ket qua tool \${toolCmd} (\${result.ms}ms, ok=\${logicalOk}): \${shortResult}\`;
      history.push({ role: "user", text: resultText.slice(0, TOOL_RESULT_CHARS + 200) });
      nextInput = \`Tiep tuc tu ket qua tool va OBSERVATION moi nhat trong lich su. Neu vua thao tac, hay kiem tra VERIFY_OBSERVATION de xac nhan dung trang thai truoc khi lam tiep. Neu xong thi final, neu can thao tac tiep thi goi tool tiep. Neu khong du chac chan thi hoi nguoi dung. Khong lap lai tool vua loi qua 2 lan.\\n\${resultText.slice(0, TOOL_RESULT_CHARS + 200)}\`;
      nextInputs.push(nextInput);
    }
    if (shouldBreakOuter) break;
    nextInput = nextInputs.join("\\n\\n");`;

block = block.replace(endPattern, newTail);

let lines = block.split(/\r?\n/);
for (let i = 0; i < lines.length; i++) {
    if (lines[i].includes('shouldBreakOuter') || lines[i].includes('nextInputs.push')) {
        continue;
    }
    if (lines[i].trim() === 'break;') {
        lines[i] = lines[i].replace('break;', 'shouldBreakOuter = true; break;');
    } else if (lines[i].trim() === 'continue;') {
        lines[i] = lines[i].replace('continue;', 'nextInputs.push(nextInput); continue;');
    }
}

// Fix here: use \n instead of \\n
let newBlock = lines.join('\n');
let newCode = code.substring(0, startIdx) + newBlock + code.substring(blockEnd);
fs.writeFileSync('server.js', newCode);
console.log("Refactored successfully");
