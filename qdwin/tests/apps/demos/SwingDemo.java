import javax.swing.*;
import java.awt.*;
import java.awt.event.*;

public class SwingDemo {
    public static void main(String[] args) {
        SwingUtilities.invokeLater(() -> {
            JFrame f = new JFrame("Swing on qdwin");
            f.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
            f.setSize(400, 300);
            JPanel p = new JPanel(new BorderLayout(10, 10));
            p.setBorder(BorderFactory.createEmptyBorder(20, 20, 20, 20));
            p.add(new JLabel("Swing demo", SwingConstants.CENTER), BorderLayout.NORTH);
            JTextField tf = new JTextField("type here");
            p.add(tf, BorderLayout.CENTER);
            JButton b = new JButton("Click me");
            b.addActionListener(e ->
                JOptionPane.showMessageDialog(f, "You typed: " + tf.getText()));
            p.add(b, BorderLayout.SOUTH);

            JMenuBar mb = new JMenuBar();
            JMenu fm = new JMenu("File");
            fm.add(new JMenuItem("New"));
            fm.add(new JMenuItem("Open"));
            fm.add(new JMenuItem("Quit"));
            JMenu em = new JMenu("Edit");
            em.add(new JMenuItem("Cut"));
            em.add(new JMenuItem("Copy"));
            em.add(new JMenuItem("Paste"));
            mb.add(fm); mb.add(em);
            f.setJMenuBar(mb);

            JPopupMenu ctx = new JPopupMenu();
            ctx.add(new JMenuItem("Context A"));
            ctx.add(new JMenuItem("Context B"));
            tf.setComponentPopupMenu(ctx);

            f.setContentPane(p);
            f.setVisible(true);
        });
    }
}
