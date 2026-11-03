package app.common;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** 오류를 openapi.yaml의 Error 형식으로 바꾼다. 내부 메시지는 응답에 넣지 않는다. */
@RestControllerAdvice
public class ApiErrorHandler {
    private static final Logger log = LoggerFactory.getLogger(ApiErrorHandler.class);

    @ExceptionHandler(ApiError.class)
    public ResponseEntity<Map<String, String>> api(ApiError e) {
        return ResponseEntity.status(e.status()).body(Map.of("code", e.code(), "message", e.getMessage()));
    }

    @ExceptionHandler(DataIntegrityViolationException.class)
    public ResponseEntity<Map<String, String>> integrity(DataIntegrityViolationException e) {
        return ResponseEntity.status(400).body(Map.of("code", "VALIDATION_FAILED", "message", "필수 값이나 값의 조건이 맞지 않습니다."));
    }

    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<Map<String, String>> unreadable(HttpMessageNotReadableException e) {
        return ResponseEntity.status(400).body(Map.of("code", "VALIDATION_FAILED", "message", "요청 형식이 올바르지 않습니다."));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, String>> unexpected(Exception e) throws Exception {
        if (e instanceof org.springframework.web.ErrorResponse) {
            throw e;   // 없는 경로·메서드 등은 Spring의 기본 응답(404·405)을 둔다
        }
        log.error("처리하지 못한 오류", e);
        return ResponseEntity.status(500).body(Map.of("code", "INTERNAL_ERROR", "message", "서버 오류가 발생했습니다."));
    }
}
