package benchmark;

public class Subject {
    public static int clamp(int value) {
        if (value < 0) {
            return 0;
        }
        return value;
    }
}
