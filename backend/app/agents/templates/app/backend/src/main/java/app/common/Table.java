package app.common;

import java.util.List;

/** 테이블 정보. created·updated는 입력·수정 때 현재 시각을 넣을 컬럼이고, joins는 조회할 때 붙일 부모 테이블이다. */
public record Table(String name, String pk, String pkType, List<String> created, List<String> updated, String joins) {
    public Table(String name, String pk, String pkType, List<String> created, List<String> updated) {
        this(name, pk, pkType, created, updated, "");
    }

    /** 같은 테이블을 부모 테이블과 함께 조회한다. */
    public Table with(String joinClause) {
        return new Table(name, pk, pkType, created, updated, joinClause);
    }

    String source() {
        return joins.isEmpty() ? name : name + " " + joins;
    }

    String key() {
        return name + "." + pk;
    }
}
