package benchmark;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

public class SubjectTest {
    @Test
    void clampsNegativeAndPreservesNonnegative() {
        assertEquals(0, Subject.clamp(-1));
        assertEquals(0, Subject.clamp(0));
        assertEquals(2, Subject.clamp(2));
    }
}
