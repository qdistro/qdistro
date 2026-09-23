// Minimal FLTK demo for qdwin compatibility test.
#include <FL/Fl.H>
#include <FL/Fl_Window.H>
#include <FL/Fl_Box.H>
#include <FL/Fl_Input.H>
#include <FL/Fl_Button.H>
#include <FL/Fl_Menu_Bar.H>
#include <FL/fl_message.H>
#include <stdio.h>

static Fl_Input *entry;

static void on_click(Fl_Widget *, void *) {
	fl_message("You typed: %s", entry->value());
}

static void on_menu_item(Fl_Widget *, void *) {
	fl_message("Menu clicked");
}

int main(int argc, char **argv) {
	Fl_Window *win = new Fl_Window(400, 300, "FLTK on qdwin");
	Fl_Menu_Bar *mb = new Fl_Menu_Bar(0, 0, 400, 25);
	mb->add("File/New", 0, on_menu_item);
	mb->add("File/Open", 0, on_menu_item);
	mb->add("File/Quit", 0, on_menu_item);
	mb->add("Edit/Cut", 0, on_menu_item);
	mb->add("Edit/Copy", 0, on_menu_item);
	mb->add("Edit/Paste", 0, on_menu_item);

	Fl_Box *box = new Fl_Box(20, 50, 360, 30, "FLTK demo");
	box->labelsize(16);

	entry = new Fl_Input(80, 100, 280, 30, "Text:");
	entry->value("type here");

	Fl_Button *btn = new Fl_Button(150, 150, 100, 35, "Click me");
	btn->callback(on_click);

	win->end();
	win->show(argc, argv);
	return Fl::run();
}
