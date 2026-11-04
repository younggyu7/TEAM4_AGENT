package app.auth;

import app.common.CurrentUser;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * 요청의 로그인 사용자: Authorization: Bearer <토큰>이 맞으면 CurrentUser를 요청에 붙인다.
 * 막는 일은 하지 않는다. 각 API가 CurrentUser.require…로 권한을 확인한다 (외부 시스템 호출은 X-Api-Key).
 */
@Component
public class AuthFilter extends OncePerRequestFilter {
    private final Accounts accounts;

    public AuthFilter(Accounts accounts) {
        this.accounts = accounts;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        String token = AuthController.token(request);
        if (token != null) {
            accounts.user(token).ifPresent(user -> request.setAttribute(CurrentUser.ATTRIBUTE, user));
        }
        chain.doFilter(request, response);
    }
}
