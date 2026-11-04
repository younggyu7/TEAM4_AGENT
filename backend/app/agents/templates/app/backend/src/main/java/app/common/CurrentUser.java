package app.common;

import jakarta.servlet.http.HttpServletRequest;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;

/**
 * 현재 사용자: 포털 로그인(app.auth)의 세션에서 읽는다 (Authorization: Bearer 토큰). 로그인하지 않았으면 역할이 빈 값.
 * id는 시연 계정에 연결한 사용자 ID다 (resources/accounts.json). 스펙의 사용자·인증 기능(TASK-003)이 정해지면 그쪽에 맞춘다.
 */
public record CurrentUser(String id, String role) {
    /** 로그인 필터가 요청에 붙이는 이름 */
    public static final String ATTRIBUTE = "app.currentUser";
    private static final String ALL = "전체 보기";

    private static HttpServletRequest request() {
        return ((ServletRequestAttributes) RequestContextHolder.currentRequestAttributes()).getRequest();
    }

    public static CurrentUser fromRequest() {
        return request().getAttribute(ATTRIBUTE) instanceof CurrentUser user ? user : new CurrentUser(null, "");
    }

    /** 외부 시스템의 호출인가: X-Api-Key가 환경 변수 SYSTEM_API_KEY와 같아야 한다. 키를 정하지 않으면 아무도 통과하지 못한다. */
    public static boolean isSystem() {
        String key = System.getenv("SYSTEM_API_KEY");
        String given = request().getHeader("X-Api-Key");
        return key != null && !key.isEmpty() && given != null
                && MessageDigest.isEqual(key.getBytes(StandardCharsets.UTF_8), given.getBytes(StandardCharsets.UTF_8));
    }

    public static void requireSystem() {
        if (!isSystem()) {
            throw new ApiError(401, "UNAUTHORIZED", "외부 시스템 인증이 필요합니다.");
        }
    }

    public void requireLogin() {
        if (role.isEmpty()) {
            throw new ApiError(401, "UNAUTHORIZED", "인증이 필요합니다.");
        }
    }

    public void require(String... roles) {
        requireLogin();
        if (!ALL.equals(role) && !Arrays.asList(roles).contains(role)) {
            throw new ApiError(403, "FORBIDDEN", "이 역할은 쓸 수 없습니다: " + role);
        }
    }
}
