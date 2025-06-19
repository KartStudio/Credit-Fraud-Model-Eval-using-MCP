from mcp.server.fastmcp import FastMCP
from typing import List
import nbformat
import pandas as pd
import sys, traceback
import os # Added for debugging CWD (can remove later if not needed)

# Initialize MCP server
mcp = FastMCP("NbReviewerv1")

# In-memory state
guidelines: List[str] = []
notebook_cells: List[dict] = []

@mcp.tool()
def load_guidelines(path: str) -> str:
    """
    Load qualitative guidelines from an Excel (.xlsx) or CSV (.csv) file.
    Only the first column is used.
    """
    global guidelines
    try:
        df = pd.read_excel(path) if path.endswith(".xlsx") else pd.read_csv(path)
        guidelines = df.iloc[:, 0].dropna().tolist()
        return f"{len(guidelines)} guideline(s) loaded."
    except Exception as e:
        return f"Error loading guidelines: {e}"

# === TOOL 2: Load Notebook from File ===
@mcp.tool()
def load_notebook(path: str) -> str:
    """
    Load a Jupyter notebook (.ipynb) from the filesystem.
    Extracts both code and markdown cells.
    """
    global notebook_cells
    try:
        nb = nbformat.read(path, as_version=4)
        # --- MODIFIED PART: Store outputs ---
        notebook_cells = [
            {"type": cell.cell_type, "source": cell.source.strip(), "outputs": cell.outputs if hasattr(cell, 'outputs') else []}
            for cell in nb.cells
        ]
        # --- END MODIFIED PART ---
        return f"{len(notebook_cells)} notebook cell(s) loaded."
    except Exception as e:
        return f"Error loading notebook: {e}"

# === TOOL 3: Load Notebook from JSON String ===
@mcp.tool()
def load_notebook_content(json_string: str) -> str:
    """
    Load notebook content from raw JSON string (e.g., pasted in Claude Desktop).
    """
    global notebook_cells
    try:
        nb = nbformat.reads(json_string, as_version=4)
        notebook_cells.clear()
        # --- MODIFIED PART: Store outputs ---
        notebook_cells.extend([
            {"type": cell.cell_type, "source": cell.source.strip(), "outputs": cell.outputs if hasattr(cell, 'outputs') else []}
            for cell in nb.cells
        ])
        # --- END MODIFIED PART ---
        return f"{len(notebook_cells)} notebook cell(s) loaded from pasted content."
    except Exception as e:
        return f"Failed to parse notebook JSON: {e}"

# === TOOL 4: Generate Claude Prompt ===
# ... (rest of your imports and initial setup) ...

@mcp.tool()
def get_prompt_for_claude(*args, **kwargs) -> str:
    """
    Generate Claude-ready prompts that pair guidelines with notebook content.
    """
    if not guidelines:
        return "No guidelines loaded. Use `load_guidelines()` first."
    if not notebook_cells:
        return "No notebook loaded. Use `load_notebook()` or `load_notebook_content()` first."

    # --- START OF TRUNCATION CONFIG (Adjust these values) ---
    MAX_LINES_PER_CODE_CELL = 15   # Max lines for code source (head + tail)
    MAX_CHARS_PER_MD_CELL = 250    # Max characters for markdown source
    MAX_OUTPUT_CHARS_STREAM = 100  # Max chars for 'stream' outputs (print, stdout)
    MAX_OUTPUT_CHARS_EXEC_RESULT = 150 # Max chars for 'execute_result' outputs
    MAX_ERROR_CHARS = 100          # Max chars for error output
    # Aim for a total prompt size that fits the 1MB display limit.
    # This will be more flexible now that notebook content isn't duplicated per guideline.
    MAX_TOTAL_NOTEBOOK_CHARS = 150000 # A more generous limit for the *single* notebook content section
                                      # (You'll still need to adjust this based on your specific notebook size)
    # --- END OF TRUNCATION CONFIG ---

    # --- MODIFIED PROMPT GENERATION LOGIC ---

    # 1. Process and truncate notebook cells (this part is mostly the same as before)
    trimmed_cells_content = []
    current_total_notebook_chars = 0

    for idx, cell in enumerate(notebook_cells):
        cell_parts = []
        cell_header = f"# Cell {idx+1} - {cell['type'].capitalize()}\n"
        
        source = cell['source'].strip()
        trimmed_source = source
        if cell['type'] == 'code':
            lines = source.split('\n')
            if len(lines) > MAX_LINES_PER_CODE_CELL:
                head_lines_len = MAX_LINES_PER_CODE_CELL // 2
                tail_lines_len = MAX_LINES_PER_CODE_CELL - head_lines_len
                trimmed_source = "\n".join(lines[:head_lines_len]) + \
                                 f"\n... [CODE TRUNCATED - {len(lines) - MAX_LINES_PER_CODE_CELL} lines omitted] ...\n" + \
                                 "\n".join(lines[-tail_lines_len:])
            cell_parts.append(trimmed_source)
        elif cell['type'] == 'markdown':
            if len(source) > MAX_CHARS_PER_MD_CELL:
                trimmed_source = source[:MAX_CHARS_PER_MD_CELL] + "\n... [MARKDOWN TRUNCATED] ..."
            cell_parts.append(trimmed_source)

        output_text_parts = []
        if cell['type'] == 'code' and 'outputs' in cell and cell['outputs']:
            for output in cell['outputs']:
                if output.output_type == 'stream':
                    output_data = output.text
                    if len(output_data) > MAX_OUTPUT_CHARS_STREAM:
                        head_len = MAX_OUTPUT_CHARS_STREAM // 2
                        tail_len = MAX_OUTPUT_CHARS_STREAM - head_len - len("\n... [STREAM OUTPUT TRUNCATED - middle omitted] ...\n")
                        if tail_len < 0: tail_len = 0
                        trimmed_output = output_data[:head_len] + \
                                         "\n... [STREAM OUTPUT TRUNCATED - middle omitted] ...\n" + \
                                         output_data[len(output_data) - tail_len:]
                        output_data = trimmed_output
                    output_text_parts.append(f"Output (stream):\n```\n{output_data}\n```")

                elif output.output_type == 'execute_result':
                    if 'text/plain' in output.data:
                        output_data = output.data['text/plain']
                        if isinstance(output_data, list):
                            output_data = "\n".join(output_data)
                        if len(output_data) > MAX_OUTPUT_CHARS_EXEC_RESULT:
                            head_len = MAX_OUTPUT_CHARS_EXEC_RESULT // 2
                            tail_len = MAX_OUTPUT_CHARS_EXEC_RESULT - head_len - len("\n... [EXEC RESULT TRUNCATED - middle omitted] ...\n")
                            if tail_len < 0: tail_len = 0
                            trimmed_output = output_data[:head_len] + \
                                             "\n... [EXEC RESULT TRUNCATED - middle omitted] ...\n" + \
                                             output_data[len(output_data) - tail_len:]
                            output_data = trimmed_output
                        output_text_parts.append(f"Output (result):\n```\n{output_data}\n```")
                    elif 'image/png' in output.data:
                        output_text_parts.append("Output (image/plot): [Image data omitted. Refer to code for generation logic.]")
                elif output.output_type == 'error':
                    output_data = f"Error Type: {output.ename}\nError Value: {output.evalue}"
                    if len(output_data) > MAX_ERROR_CHARS:
                        output_data = output_data[:MAX_ERROR_CHARS] + "\n... [ERROR TRUNCATED] ..."
                    output_text_parts.append(f"Output (error):\n```\n{output_data}\n```")
        
        cell_content = "\n\n".join(cell_parts + output_text_parts)

        # Apply MAX_TOTAL_NOTEBOOK_CHARS here to the entire notebook content string
        if current_total_notebook_chars + len(cell_header) + len(cell_content) > MAX_TOTAL_NOTEBOOK_CHARS:
            trimmed_cells_content.append(cell_header + "[Remaining cell content and subsequent cells skipped due to overall notebook content length limit.]")
            break
        else:
            trimmed_cells_content.append(cell_header + cell_content)
            current_total_notebook_chars += len(cell_header) + len(cell_content)

    final_notebook_content_str = "\n\n".join(trimmed_cells_content)

    # 2. Build the final prompt for Claude *once*, putting notebook content first
    prompt = "Below is the content of a Jupyter notebook. Please review this notebook based on the specific guidelines provided afterwards.\n"
    prompt += "Note that some code cells, markdown cells, or their outputs may be truncated due to length limits.\n\n"
    prompt += "--- START NOTEBOOK CONTENT ---\n"
    prompt += final_notebook_content_str
    prompt += "\n--- END NOTEBOOK CONTENT ---\n\n"

    prompt += "--- REVIEW GUIDELINES ---\n"
    for i, guideline in enumerate(guidelines, 1):
        prompt += f"Guideline {i}: {guideline}\n"
    prompt += "--- END REVIEW GUIDELINES ---\n\n"

    prompt += "--- ANALYSIS INSTRUCTIONS ---\n"
    prompt += "Please evaluate the notebook content based on the guidelines. For each guideline, state whether it is met or not, and provide specific, constructive feedback and reasoning. Cite specific cell numbers where relevant.\n"
    prompt += "Example feedback format:\n"
    prompt += "Guideline X: [Met/Not Met]\n"
    prompt += "Feedback: [Your specific feedback, referencing relevant cell numbers]\n\n"
    prompt += "--- END ANALYSIS INSTRUCTIONS ---\n"

    return prompt.strip()
    
# === RESOURCE: Help Message ===
@mcp.resource("help://reviewer")
def get_help() -> str:
    return (
        "Notebook Reviewer MCP Help:\n\n"
        "1. Use `load_guidelines(path)` to load review guidelines from an Excel or CSV file.\n"
        "2. Load notebook in either of two ways:\n"
        "   - `load_notebook(path)` for local .ipynb files\n"
        "   - `load_notebook_content(json_string)` for raw notebook JSON from Claude\n"
        "3. Use `get_prompt_for_claude()` to generate the full review prompt.\n"
        "Paste the result into Claude Desktop for feedback.\n"
    )

# === ENTRY POINT ===
if __name__ == "__main__":
    try:
        # Added for CWD debugging - you can remove this after confirming CWD
        current_working_directory = os.getcwd()
        print(f"Server's Current Working Directory: {current_working_directory}", file=sys.stderr, flush=True)
        print("🟢 Starting MCP...", file=sys.stderr, flush=True)
        mcp.run()
    except Exception as e:
        print("🔥 MCP crashed:", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)