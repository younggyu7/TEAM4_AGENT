package app.auth;

import app.common.ApiError;
import jakarta.servlet.http.HttpServletRequest;
import java.util.Map;
import org.springframework.http.CacheControl;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 포털 로그인 (서비스 틀). 스펙의 업무 API(/api/v1)와 따로 둔다. */
@RestController
@RequestMapping("/api/auth")
public class AuthController {
    public record LoginRequest(String loginId, String password) {}

    private final Accounts accounts;

    public AuthController(Accounts accounts) {
        this.accounts = accounts;
    }

    static String token(HttpServletRequest request) {
        String header = request.getHeader("Authorization");
        return header != null && header.startsWith("Bearer ") ? header.substring(7).trim() : null;
    }

    @PostMapping("/login")
    public ResponseEntity<Map<String, String>> login(@RequestBody LoginRequest body) {
        return ResponseEntity.ok().cacheControl(CacheControl.noStore()).body(accounts.login(body.loginId(), body.password()));
    }

    @GetMapping("/me")
    public ResponseEntity<Map<String, String>> me(HttpServletRequest request) {
        Map<String, String> me = accounts.me(token(request)).orElseThrow(() -> new ApiError(401, "UNAUTHORIZED", "다시 로그인하세요."));
        return ResponseEntity.ok().cacheControl(CacheControl.noStore()).body(me);
    }

    @PostMapping("/logout")
    public ResponseEntity<Void> logout(HttpServletRequest request) {
        accounts.logout(token(request));
        return ResponseEntity.noContent().build();
    }
}
