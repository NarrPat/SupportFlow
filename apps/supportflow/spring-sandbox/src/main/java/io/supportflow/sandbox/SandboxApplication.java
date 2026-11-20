package io.supportflow.sandbox;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class SandboxApplication {
    public static void main(String[] args) {
        var context = SpringApplication.run(SandboxApplication.class, args);
        if (context.getEnvironment().matchesProfiles("migrate")) context.close();
    }
}
