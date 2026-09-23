#!/usr/bin/env python3
"""Minimal Tk demo for qdwin compatibility test."""
import tkinter as tk
from tkinter import ttk, messagebox

root = tk.Tk()
root.title("Tk on qdwin")
root.geometry("400x300")

frame = ttk.Frame(root, padding=20)
frame.pack(fill=tk.BOTH, expand=True)

ttk.Label(frame, text="Tkinter demo", font=("Sans", 14, "bold")).pack(pady=10)

entry = ttk.Entry(frame)
entry.pack(fill=tk.X, pady=5)
entry.insert(0, "type here")

def on_btn():
    messagebox.showinfo("Hello", f"You typed: {entry.get()}")

ttk.Button(frame, text="Click me", command=on_btn).pack(pady=10)

# Right-click context menu
menu = tk.Menu(root, tearoff=0)
menu.add_command(label="Cut")
menu.add_command(label="Copy")
menu.add_command(label="Paste")
def show_menu(e):
    try:
        menu.tk_popup(e.x_root, e.y_root)
    finally:
        menu.grab_release()
frame.bind("<Button-3>", show_menu)

root.mainloop()
