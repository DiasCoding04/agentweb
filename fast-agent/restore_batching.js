const fs = require('fs');
let code = fs.readFileSync('server.js', 'utf8');

const startTargetIdx = code.indexOf('    const tool = model.parsed.tool;');
const endTargetIdx = code.indexOf('  if (taskState.finalStatus === "RUNNING")');

if (startTargetIdx === -1 || endTargetIdx === -1) {
  console.log('Targets not found');
  process.exit(1);
}

const originalBlock = code.slice(startTargetIdx + 35, endTargetIdx);

// Remove the `if (!tool || !tool.cmd) throw new Error("JSON không có final hoặc tool.cmd.");`
const blockBodyStart = originalBlock.indexOf('    const toolArgs =');
if (blockBodyStart === -1) {
    console.log('Could not find start of block body');
    process.exit(1);
}

const blockBody = originalBlock.slice(blockBodyStart);

let newBlock = blockBody.replace(/break;/g, 'shouldBreakOuter = true; break;');

const replacement = `    const tools = model.parsed.tools || (model.parsed.tool ? [model.parsed.tool] : []);
    if (!tools.length) throw new Error("JSON không có final hoặc tools.");

    history.push({ role: "model", text: JSON.stringify(model.parsed) });

    let nextInputs = [];
    let shouldBreakOuter = false;

    for (const tool of tools) {
      if (!tool || !tool.cmd) continue;

` + newBlock + `
    }
    
    if (nextInputs.length > 0) {
      nextInput = nextInputs.join("\\n");
    }
    
    if (shouldBreakOuter) {
      break;
    }

`;

const newCode = code.slice(0, startTargetIdx) + replacement + code.slice(endTargetIdx);
fs.writeFileSync('server.js', newCode);
console.log('Restored batching successfully.');
