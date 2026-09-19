"""Local, masked entry for the bot's API key. Never prints the key."""

from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from dotenv import set_key


ROOT = Path(__file__).resolve().parent


def save_key(key):
    key = key.strip()
    if not key.startswith("sk-") or len(key) < 20 or any(char.isspace() for char in key):
        raise ValueError("Enter a valid OpenAI API key beginning with sk-.")
    path = ROOT / ".env"
    if not path.is_file():
        raise ValueError("The project's .env file is missing. Set up Slack first.")
    set_key(path, "LABPURCHASE_OPENAI_API_KEY", key)
    set_key(path, "LABPURCHASE_API_MODEL", "gpt-5.6-luna")
    set_key(path, "LABEL_READER_ENABLED", "true")


def main():
    window = tk.Tk()
    window.title("LabPurchaseBot — API setup")
    window.geometry("530x230")
    window.resizable(False, False)
    tk.Label(window, text="Paste the bot's OpenAI API key here", font=("Segoe UI", 12)).pack(pady=(20, 8))
    tk.Label(window, text="Saved only in this project's ignored .env file.\nAPI billing is separate from your Codex allowance.").pack()
    entry = tk.Entry(window, show="*", width=65)
    entry.pack(pady=15)
    entry.focus_set()

    def save():
        try:
            save_key(entry.get())
        except ValueError as error:
            messagebox.showerror("API setup", str(error))
            return
        except Exception:
            messagebox.showerror("API setup", "Could not save the key. Check access to the project folder.")
            return
        entry.delete(0, tk.END)
        messagebox.showinfo("API setup", "Saved locally. The running bot will detect the key shortly. A live API check is still needed.")
        window.destroy()

    tk.Button(window, text="Save API key locally", command=save).pack()
    window.mainloop()


if __name__ == "__main__":
    main()
