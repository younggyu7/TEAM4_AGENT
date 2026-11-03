package app.common;

import jakarta.servlet.http.HttpServletRequest;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;

/**
 * 현재 사용자. 시연용으로 헤더(X-User-Id, X-User-Role)에서 읽는다.
 * TODO(TASK-003): 운영 전에 실제 인증(openapi.yaml의 bearerAuth)으로 바꾼다.
 */
public record CurrentUser(String id, String role) {
    private static final String ALL = "전체 보기";

    private static HttpServletRequest request() {
        return ((ServletRequestAttributes) RequestContextHolder.currentRequestAttributes()).getRequest();
    }

    public static CurrentUser fromRequest() {
        HttpServletRequest request = request();
        String role = request.getHeader("X-User-Role");
        return new CurrentUser(request.getHeader("X-User-Id"), role == null ? "" : URLDecoder.decode(role, StandardCharsets.UTF_8));
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
